"""Encrypted tombstones: write, replay, test membership (EF-BIB-053, EF-BIB-054, ENF-SEC-005).

**Write** (`record`, called by the purge): one row of `content_tombstone` sealed for `db`, and one
line per read-write root sealed for that root's `root_id`. The database transaction holds the row,
the registry entry of the kid (`tombstone_kid`) and one `tombstone_pending_root` row per root: the
line exists durably before any disk is touched. Then, per mounted root, fsops adds the kid to the
sentinel's registry, appends the line, and the pending row goes. An absent root, or a crash in
between, leaves the pending row, written first at the next replay (GAP-12-14).

**Replay** (`replay`, at start-up and after a restore, before any reconcile or re-adoption):
pending lines first, then the table and every mounted journal, line by line:

- the kid is in the keyring and the line opens under its root (or a known predecessor root,
  `tombstone.root_mismatch`): the tombstone **blocks**; a journal line missing from the table is
  merged into it;
- the kid is in the keyring but the line does not open (altered, copied from another root,
  forged), or the line is malformed: **inert**, `tombstone.unverified`;
- the kid is not in the keyring but is in the registry (the database's, or an entry of the root's
  sentinel whose tag verifies under a key we hold, review PR #52): the
  reacquisition of its scope on that root **blocks**, and so does reconcile, `tombstone.key_unknown`
  (a backup older than a rotation, keyring not imported), until `secrets import` or the typed
  "lost keyring" waiver, after which the line is **inert**;
- the kid is in neither registry: forged, **inert**, `tombstone.unverified` with `kid_unregistered`.

An inert line never blocks and never deletes. Nothing here deletes anything: a deletion that a
replay asks for is the purge's, after the admin confirmed it (EF-INT-026, L34/L35).

**Chain** (M1b L26, EF-BIB-054 (6)): each line written moves the root's chained label
(`chain.ChainState`, kept in the database and, authenticated, in the sentinel). A replay that finds
the journal short of it (a line cut off the end, or one removed) marks the root
`tombstone_journal_truncated`: `tombstone.journal_truncated`, no reconcile and no reacquisition on
that root until `tombstone set-aside-journal` writes the journal again from the table (D12).
Reordering and repeating lines change nothing. **Rekey** (`tombstone rekey`, after a key leak):
the table, the owed lines and every journal are resealed under the active key; a journal is
replaced through a new file and one atomic `rename`, under the lock that serializes every append,
so a line recorded meanwhile is in the new file.

**Membership** (`check`): against the in-memory index only; it fails closed until a replay ran.
"""

import hashlib
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.identity.public import (
    DEFAULT_FRESH_AUTH_S,
    RESERVED_ACTIONS,
    Allowed,
    Principal,
    authorize_reserved,
)
from shelfhold.infrastructure.event_sequence import invalidate_tokens
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, coherence, identity, platform
from shelfhold.infrastructure.secrets import registry as key_registry
from shelfhold.infrastructure.secrets.errors import EnvelopeError
from shelfhold.infrastructure.secrets.keyring import Key, Keyring
from shelfhold.infrastructure.secrets.tombstone_aead import (
    ALG,
    DB_ROOT,
    chain_mac,
    kid_tag,
    open_values,
    seal_values,
    verify_chain_mac,
    verify_kid_tag,
)
from shelfhold.library.tombstones.chain import EMPTY, ChainState, judge, tag_of, tip_of
from shelfhold.library.tombstones.cited import default_normalizers
from shelfhold.library.tombstones.index import (
    LIFT_MARK,
    NORMALIZED_FIELDS,
    HashWith,
    MembershipIndex,
    Normalizers,
    flatten,
)
from shelfhold.library.tombstones.journal import (
    MAX_JOURNAL_BYTES,
    JournalFormatError,
    JournalLine,
    header_bytes,
    parse_journal,
    parse_line,
    parse_lines_ignoring_header,
)
from shelfhold.library.tombstones.scopes import EmptyRefError, alias_family, fold_ref
from shelfhold.library.tombstones.lift import (
    LIFT_REASON,
    LiftConfirmationError,
    LiftRefusedError,
    TombstoneRow,
    UnknownTombstoneError,
    lift_phrase,
)
from shelfhold.library.tombstones.state import TombstoneState
from shelfhold.library.tombstones.values import (
    ContentHash,
    Scope,
    SeriesBindingRef,
    TombstoneValueError,
    TombstoneValues,
    VariantRef,
    decode_plaintext,
    encode_plaintext,
)

log = structlog.get_logger(__name__)

TABLE_FORMAT: Final = 2  # `content_tombstone.v`: format 2.0, the same associated data as a line
LOST_KEYRING_PHRASE: Final = "the keyring is lost"
# Under `restored_behind` the waiver is the way out of a deadlock (no export, the adoption is
# blocked by `key_unknown`): the phrase states what it costs.
FROZEN_LOST_KEYRING_PHRASE: Final = (
    "the keyring is lost, its tombstones become inert and purged content may come back"
)
INERT_REASONS: Final = frozenset(
    {"authentication_failed", "kid_unregistered", "lost_keyring", "malformed"}
)
# `content_tombstone.reason`: a closed list, never free text (it could carry a title, I36).
REASONS: Final = frozenset({"purge_unit", "purge_work", "replayed", "new_form", "lifted"})
_LOST_KEYRING_ACTION = RESERVED_ACTIONS["tombstone.accept_lost_keyring"]
_SET_ASIDE_ACTION = RESERVED_ACTIONS["tombstone.set_aside_journal"]
_REKEY_ACTION = RESERVED_ACTIONS["tombstone.rekey"]
_LIFT_ACTION = RESERVED_ACTIONS["tombstone.lift"]
# One lock for every write of a journal and of the label that follows it: an append never
# interleaves with a rewrite (`tombstone rekey`), whichever service object makes it. It excludes
# THREADS of one process only. Between processes the exclusion is the `flock` on `/data/.lock`
# (the instance lock, held by the server or by the operating command), which this lock relies on.
_WRITE_LOCK = threading.RLock()
_REWRITE_ATTEMPTS = 3
JOURNAL_PATH: Final = ".shelfhold/tombstones"  # `physical_roots.TOMBSTONE_JOURNAL`
REWRITE_PATH: Final = ".shelfhold/tombstones.rekey"  # the new file of a rewrite

_tomb, _kid, _lost = (
    coherence.content_tombstone,
    coherence.tombstone_kid,
    coherence.tombstone_lost_kid,
)
_pending = coherence.tombstone_pending_root


class TombstoneGate(Protocol):
    """What a cycle that could bring content back needs: may this candidate be acquired?"""

    def check(
        self, candidate: TombstoneValues, *, root_id: str | None, hash_with: HashWith | None = None
    ) -> "Verdict": ...


class JournalDisk(Protocol):
    """The part of `Fsops` the tombstones use (fsops stays the only writer)."""

    def append_tombstone_line(self, root_path: str, line: bytes, *, header: bytes) -> bool | None:
        """True when the line created the file."""
        ...

    def replace_tombstone_journal(self, root_path: str, new: bytes, expected: bytes) -> bool:
        """False, nothing replaced, when the journal is no longer `expected`."""
        ...

    def record_tombstone_chain(self, root_path: str, chain: dict[str, Any]) -> bool: ...

    def tombstone_chain_written(self, root_path: str) -> bool: ...

    def tombstone_chain(self, root_path: str) -> dict[str, Any] | None: ...

    def record_tombstone_kid(self, root_path: str, kid: str, tags: dict[str, str]) -> None: ...

    def read_tombstone_journal(self, root_path: str, *, max_bytes: int) -> bytes | None: ...

    def set_aside_tombstone_journal(self, root_path: str, stamp: str) -> str | None: ...

    def tombstone_kids(self, root_path: str) -> dict[str, dict[str, str]]: ...


@dataclass(frozen=True)
class TombstoneRoot:
    """A read-write physical root (`physical_root`), mounted or not."""

    root_id: str
    path: str
    mounted: bool = True
    predecessor_root_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recorded:
    seq: int
    kid: str
    written: tuple[str, ...]  # root_ids whose journal has the line
    pending: tuple[str, ...]  # root_ids that will get it when they come back


@dataclass(frozen=True)
class Lifted:
    seq: int  # the tombstone that was lifted
    line_seq: int  # the line that lifts it
    written: tuple[str, ...]
    pending: tuple[str, ...]


@dataclass(frozen=True)
class InertLine:
    root_id: str  # `db` for a row of the table
    seq: int | None  # None when the line did not even parse
    reason: str
    line: int | None = None  # line number in the journal of a line that did not parse


@dataclass
class ReplayReport:
    verified: int = 0
    merged: int = 0
    flushed: int = 0
    inert: list[InertLine] = field(default_factory=list)
    key_unknown: set[tuple[str, str]] = field(default_factory=set)  # (root_id or db, scope)
    unreadable_roots: set[str] = field(default_factory=set)
    root_mismatch: set[str] = field(default_factory=set)
    truncated_roots: set[str] = field(default_factory=set)  # the journal stops short of its chain
    truncated_new: set[str] = field(default_factory=set)  # ... and nobody was told yet
    chain_ok: set[str] = field(default_factory=set)  # roots whose journal was judged, and is whole


@dataclass(frozen=True)
class Verdict:
    blocked: bool
    cause: str | None = None  # tombstoned | key_unknown | journal_unreadable | journal_truncated


class SetAsideRefusedError(RuntimeError):
    """The damaged journal holds lines that cannot be authenticated with the keyring at hand:
    setting it aside could let purged content come back. Restore the keyring first."""


@dataclass(frozen=True)
class RekeyReport:
    rewritten: dict[str, int]  # root id -> lines resealed in its journal
    owed: tuple[str, ...]  # roots whose journal is rewritten when they are replayed
    table_rows: int  # rows of the database resealed (the owed lines included)


class NotReplayedError(RuntimeError):
    """Membership asked before the first replay: nothing may be reacquired yet."""


class LostKeyringConfirmationError(ValueError):
    """The typed confirmation of `tombstone accept-lost-keyring` does not match."""


class LostKeyringRefusedError(PermissionError):
    """The principal may not waive the lost keyring (reserved action: admin, session or CLI)."""

    def __init__(self, verdict: object) -> None:
        super().__init__(f"tombstone.accept_lost_keyring refused: {type(verdict).__name__}")
        self.verdict = verdict


def _identity(values: TombstoneValues, cited: Mapping[str, str]) -> tuple[object, ...]:
    return (values, tuple(sorted(cited.items())))


def _not_lifted(
    entries: Sequence[tuple[TombstoneValues, dict[str, str]]],
) -> list[tuple[TombstoneValues, dict[str, str]]]:
    """What still blocks: the entries in order, a lift taking out the earlier entries of the same
    values and cited versions (a purge laid after the lift is back in)."""
    active: list[tuple[TombstoneValues, dict[str, str]]] = []
    for values, cited in entries:
        if LIFT_MARK in cited:
            base = {k: v for k, v in cited.items() if k != LIFT_MARK}
            active = [item for item in active if item != (values, base)]
        else:
            active.append((values, cited))
    return active


class TombstoneService:
    def __init__(
        self,
        engine: Engine,
        keyring: Keyring,
        disk: JournalDisk,
        bus: EventBus,
        *,
        normalizers: Normalizers | None = None,
    ) -> None:
        self._engine = engine
        self._keyring = keyring
        self._disk = disk
        self._bus = bus
        self._normalizers = normalizers or default_normalizers()
        self._index: MembershipIndex | None = None
        self._alias_signature: tuple[Any, ...] | None = None
        self._alias_cache: dict[str, list[tuple[str, str, str, str]]] = {}
        self._report = ReplayReport()
        self._sentinel_kids: dict[str, frozenset[str]] = {}  # authenticated entries only
        self._state = TombstoneState(engine)
        self._fresh_auth_s = DEFAULT_FRESH_AUTH_S
        self._hardened = False

    def use_reserved_policy(self, *, fresh_auth_s: int, hardened: bool) -> None:
        """The settings the reserved actions of this service are judged under."""
        self._fresh_auth_s = fresh_auth_s
        self._hardened = hardened

    def _authorize(self, principal: Principal, action: Any, now: datetime) -> None:
        verdict = authorize_reserved(
            principal, action, now=now, fresh_auth_s=self._fresh_auth_s, hardened=self._hardened
        )
        if not isinstance(verdict, Allowed):
            raise LostKeyringRefusedError(verdict)

    def __repr__(self) -> str:
        return f"TombstoneService(index={self._index!r})"

    def use_keyring(self, keyring: Keyring) -> None:
        """After `secrets import` or a rotation; the caller replays next."""
        self._keyring = keyring

    def use_disk(self, disk: JournalDisk) -> None:
        """The `Fsops` of the moment (it carries the restore freeze and the gates of this call)."""
        self._disk = disk

    @property
    def report(self) -> ReplayReport:
        return self._report

    # -- write ------------------------------------------------------------------------------

    def cited_now(self, values: TombstoneValues) -> dict[str, str]:
        """The normalizers in force for the normalized fields these values have."""
        names = {NORMALIZED_FIELDS[f] for f in flatten(values) if f in NORMALIZED_FIELDS}
        return {name: self._normalizers.current_version(name) for name in sorted(names)}

    def _line(self, values: bytes, scope: str, root_id: str, seq: int) -> bytes:
        key = self._keyring.active
        nonce, ct = seal_values(key.material, values, scope=scope, root=root_id, seq=seq)
        return JournalLine(seq, scope, key.kid, nonce, ct).dumps()

    def _kid_tags(self, root_id: str, kid: str) -> dict[str, str]:
        """The entry of `kid` in a root's sentinel, tagged under every key of the keyring: a
        restored keyring that misses the newest key still verifies it under an older one."""
        return {key.kid: kid_tag(key.material, root_id, kid) for key in self._keyring.keys}

    def _authenticated(self, root_id: str, entries: Mapping[str, Mapping[str, str]]) -> set[str]:
        """Registry entries of a sentinel whose tag verifies under a key we hold."""
        verified: set[str] = set()
        for kid, tags in entries.items():
            for signer, tag in tags.items():
                key = self._keyring.find(signer)
                if key is not None and verify_kid_tag(key.material, root_id, kid, tag):
                    verified.add(kid)
                    break
        return verified

    def _register_kid(self, connection: Connection, kid: str, seq: int) -> None:
        connection.execute(
            sa.insert(_kid).values(kid=kid, first_seq=max(seq, 1)).prefix_with("OR IGNORE")
        )

    def _insert_row(
        self, connection: Connection, plaintext: bytes, scope: str, *, reason: str | None
    ) -> int:
        seq = self._next_seq(connection)
        key = self._keyring.active
        key_registry.register_active_key(connection, self._keyring)
        self._register_kid(connection, key.kid, seq)
        nonce, ct = seal_values(key.material, plaintext, scope=scope, root=DB_ROOT, seq=seq)
        epoch = connection.execute(sa.select(platform.instance.c.restore_epoch)).scalar()
        connection.execute(
            sa.insert(_tomb).values(
                id=new_ulid(), v=TABLE_FORMAT, scope=scope, seq=seq, kid=key.kid, alg=ALG,
                nonce=nonce, ct=ct, epoch=int(epoch or 0), reason=reason,
            )
        )  # fmt: skip
        return seq

    @staticmethod
    def _next_seq(connection: Connection) -> int:
        in_table = connection.execute(sa.select(sa.func.max(_tomb.c.seq))).scalar()
        pending = connection.execute(sa.select(sa.func.max(_pending.c.seq))).scalar()
        return max(int(in_table or 0), int(pending or 0)) + 1

    def record(
        self,
        values: TombstoneValues,
        roots: Sequence[TombstoneRoot],
        *,
        reason: str | None = None,
    ) -> Recorded:
        return self._record(values, roots, reason=reason, cited=self.cited_now(values))

    def _record(
        self,
        values: TombstoneValues,
        roots: Sequence[TombstoneRoot],
        *,
        reason: str | None,
        cited: Mapping[str, str],
        lifting: bool = False,
        within: Callable[[Connection], None] | None = None,
    ) -> Recorded:
        """A tombstone, or (`lifting`) the line that lifts the tombstone of the same values and
        cited versions: the same row, the same line in every journal, the mark in `cited`.
        `within` runs in the transaction that writes the row (the audit of a lift)."""
        if reason is not None and reason not in REASONS:
            raise ValueError(f"reason is one of {sorted(REASONS)}")
        stored = {**cited, LIFT_MARK: "1"} if lifting else dict(cited)
        plaintext = encode_plaintext(values, stored)
        kid = self._keyring.active.kid
        with self._engine.begin() as connection:
            seq = self._insert_row(connection, plaintext, values.scope, reason=reason)
            lines = {
                root.root_id: self._line(plaintext, values.scope, root.root_id, seq)
                for root in roots
            }
            for root_id, line in lines.items():
                connection.execute(
                    sa.insert(_pending).values(root_id=root_id, seq=seq, line=line.decode("ascii"))
                )
            if within is not None:
                within(connection)
        if self._index is not None:  # the row is committed: it blocks (or frees) now
            if lifting:
                self._index.remove(values, cited)
            else:
                self._index.add_all([(values, dict(cited))])
        written: list[str] = []
        for root in roots:
            if root.mounted and self._write_line(root, seq, kid, lines[root.root_id]):
                written.append(root.root_id)
        pending = tuple(r.root_id for r in roots if r.root_id not in written)
        # the scope and the counts: the values are sealed in the table and must never be logged
        log.debug("tombstone.recorded", scope=values.scope, seq=seq, written=len(written),
                  pending=len(pending), lifting=lifting)  # fmt: skip
        return Recorded(seq, kid, tuple(written), pending)

    # -- `tombstone lift` (06 Q54): see `lift.py` -------------------------------------------

    def _active(self, connection: Connection) -> dict[int, tuple[TombstoneValues, dict[str, str]]]:
        """The tombstones of the table that are not lifted: `seq -> (values, cited)`, read in
        order, a lift taking out what it names (a purge laid after it counts again)."""
        active: dict[int, tuple[TombstoneValues, dict[str, str]]] = {}
        rows = connection.execute(
            sa.select(_tomb.c.scope, _tomb.c.seq, _tomb.c.kid, _tomb.c.nonce, _tomb.c.ct).order_by(
                _tomb.c.seq
            )
        ).all()
        for scope, seq, kid, nonce, ct in rows:
            if self._keyring.find(kid) is None:
                continue  # inert or unknown: not a tombstone that can be lifted here
            try:
                values, cited = self._open(scope, kid, bytes(nonce), bytes(ct), DB_ROOT, seq)
            except (EnvelopeError, TombstoneValueError):
                continue
            if LIFT_MARK in cited:
                base = {k: v for k, v in cited.items() if k != LIFT_MARK}
                for other in [n for n, item in active.items() if item == (values, base)]:
                    del active[other]
            else:
                active[seq] = (values, cited)
        return active

    def listing(self) -> list[TombstoneRow]:
        """What the admin needs to choose a sequence to lift: the number, the day, the scope and
        the reason, of the tombstones that still block. Read from the clear columns of the table:
        no sealed value is opened for the answer (the variant, the place, the title and the path
        are not stored anywhere else, and this lists none)."""
        with self._engine.connect() as connection:
            alive = set(self._active(connection))
            rows = connection.execute(
                sa.select(_tomb.c.seq, _tomb.c.created_at, _tomb.c.scope, _tomb.c.reason).order_by(
                    _tomb.c.seq
                )
            ).all()
        return [
            TombstoneRow(seq, made, scope, reason)
            for seq, made, scope, reason in rows
            if seq in alive
        ]

    def lift(
        self,
        roots: Sequence[TombstoneRoot],
        *,
        seq: int,
        confirmation: str,
        principal: Principal,
        now: datetime,
    ) -> Lifted:
        """Reserved action (checked here too, not only at the door), typed phrase `lift <seq>`,
        audited with the actor and the sequence, no value. The tombstone no longer blocks; nothing
        is restored; the line that lifts it is owed to every root, like a tombstone."""
        verdict = authorize_reserved(
            principal, _LIFT_ACTION, now=now, fresh_auth_s=self._fresh_auth_s,
            hardened=self._hardened,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):
            raise LiftRefusedError(verdict)
        if confirmation != lift_phrase(seq):
            raise LiftConfirmationError(f"type the phrase exactly: {lift_phrase(seq)}")
        with _WRITE_LOCK:  # two lifts of one tombstone at once: the second finds it lifted
            with self._engine.connect() as connection:
                target = self._active(connection).get(seq)
            if target is None:
                raise UnknownTombstoneError(f"no tombstone to lift at sequence {seq}")
            values, cited = target

            def audit(connection: Connection) -> None:
                connection.execute(
                    sa.insert(identity.audit).values(
                        id=new_ulid(), actor_id=principal.actor_id, action="tombstone.lifted",
                        target=f"seq:{seq}", session_id=principal.session_id,
                        auth_time=principal.auth_time,
                    )
                )  # fmt: skip
                invalidate_tokens(connection)  # no removal is told: the clients resync

            line = self._record(
                values, roots, reason=LIFT_REASON, cited=cited, lifting=True, within=audit
            )
            return Lifted(seq, line.seq, line.written, line.pending)

    def _write_line(self, root: TombstoneRoot, seq: int, kid: str, line: bytes) -> bool:
        """Registry of the sentinel first, then the line, then the label that follows it, then the
        pending row goes. Lines of a root are written in `seq` order: one that has an older line
        still owed waits for it."""
        with _WRITE_LOCK:
            with self._engine.connect() as connection:
                older = connection.execute(
                    sa.select(_pending.c.seq)
                    .where(_pending.c.root_id == root.root_id, _pending.c.seq < seq)
                    .limit(1)
                ).first()
            if older is not None:
                return False
            try:
                self._disk.record_tombstone_kid(root.path, kid, self._kid_tags(root.root_id, kid))
                created = bool(
                    self._disk.append_tombstone_line(root.path, line, header=header_bytes())
                )
            except (OSError, ValueError, RuntimeError) as error:
                log.warning("tombstone.line_pending", root_id=root.root_id,
                            reason=type(error).__name__)  # fmt: skip
                return False  # stays pending: written at the next replay of this root
            self._advance_chain(root, seq, line, created=created)
            with self._engine.begin() as connection:
                connection.execute(
                    sa.delete(_pending).where(
                        _pending.c.root_id == root.root_id, _pending.c.seq == seq
                    )
                )
        return True

    # -- the chained label (EF-BIB-054 (6)) -------------------------------------------------

    def _advance_chain(self, root: TombstoneRoot, seq: int, line: bytes, *, created: bool) -> None:
        """The label after the line just appended (under the write lock). A journal that this
        call created starts from nothing; one that was not tracked stays untracked (the next
        replay takes it as it is); a line that is not above the last `seq` is one written again
        (a root stamped again), and the label is then rebuilt by the next replay."""
        previous = self._state.chain(root.root_id)
        if previous is None:
            if not created:
                return
            previous = EMPTY
        if seq <= previous.seq:
            self._state.set_chain(root.root_id, None)
            return
        current = previous.advanced(seq, tag_of(parse_line(line[:-1]).ct))
        self._state.set_chain(root.root_id, current)
        self._mirror(root, current)

    def _signers(self) -> list[Key]:
        """The keys that may vouch for a label: every key we hold that did not leak."""
        return [key for key in self._keyring.keys if key.state != "compromised"]

    def _mirror(self, root: TombstoneRoot, state: ChainState) -> None:
        """Copy the label to the sentinel and, when that was made, remember it in the database
        (`mirrored`): from then on a sentinel that shows no valid chain was stripped."""
        if self._copy_chain(root, state):
            self._state.set_chain(
                root.root_id, ChainState(state.seq, state.count, state.label, mirrored=True)
            )

    def _copy_chain(self, root: TombstoneRoot, state: ChainState) -> bool:
        """The label, authenticated under every sound key, in the sentinel of the root. Best
        effort: the database is the authority, a replay completes the copy. True when copied."""
        tags = {
            key.kid: chain_mac(key.material, root.root_id, state.seq, state.count, state.label)
            for key in self._signers()
        }
        chain = {
            **{k: v for k, v in state.to_json().items() if k not in ("pending", "mirrored")},
            "tags": tags,
        }
        try:
            self._disk.record_tombstone_chain(root.path, chain)
        except (OSError, ValueError, RuntimeError) as error:
            log.debug("tombstone.chain_copy_deferred", root_id=root.root_id,
                      reason=type(error).__name__)  # fmt: skip
            return False
        return True

    def _sentinel_chain(self, root: TombstoneRoot) -> tuple[ChainState | None, bool]:
        """The label the sentinel carries, if its MAC verifies under a key we hold, and whether
        it does under a sound one (not compromised). Only a sound one binds a journal; one that
        only a leaked key vouches for still proves the field was not stripped or replaced by
        noise (between `mark_compromised` and the rewrite, that is all the sentinel has)."""
        raw = self._disk.tombstone_chain(root.path)
        state = ChainState.from_json(raw)
        if state is None or raw is None or not isinstance(raw.get("tags"), dict):
            return None, False
        found: ChainState | None = None
        for signer, mac in raw["tags"].items():
            key = self._keyring.find(signer) if isinstance(signer, str) else None
            # the label was signed for the root id the sentinel had then: a root that took over
            # a predecessor's place (restore, takeover) carries the old one
            if key is not None and any(
                verify_chain_mac(key.material, rid, state.seq, state.count, state.label, mac)
                for rid in (root.root_id, *root.predecessor_root_ids)
            ):
                found = ChainState(state.seq, state.count, state.label)
                if key.state != "compromised":
                    return found, True
        return found, False

    def _signed_by_absent_keys(self, root: TombstoneRoot) -> bool:
        """The sentinel's chain is vouched for only by keys that the registries know and the
        keyring lacks (a keyring that was lost): it cannot be checked, and the root is already
        held by `key_unknown` or has waived those keys. Kids nobody ever issued do not count."""
        raw = self._disk.tombstone_chain(root.path)
        tags = raw.get("tags") if isinstance(raw, dict) else None
        if not isinstance(tags, dict) or not tags:
            return False
        with self._engine.connect() as connection:
            known = set(connection.execute(sa.select(_kid.c.kid)).scalars())
            known |= set(connection.execute(sa.select(_lost.c.kid)).scalars())
        return all(
            isinstance(signer, str) and signer in known and self._keyring.find(signer) is None
            for signer in tags
        )

    def _judge_chain(
        self,
        root: TombstoneRoot,
        tags: Mapping[int, bytes],
        report: ReplayReport,
        *,
        write: bool = True,
    ) -> bool:
        """The journal of `root`, whose authentic lines carry `tags` (seq -> AEAD tag), against
        the label the database and the sentinel hold. True when whole; a truncated one is
        reported. A whole one brings both records up to its tip (`write`)."""
        database = self._state.chain(root.root_id)
        shown, sound = self._sentinel_chain(root)
        sentinel = shown if sound else None
        # Fail closed: a sentinel that has carried a chain (or a database that tracks the
        # journal) and now shows none that verifies was stripped or its MAC broken, which is what
        # a cut after a restore needs. The same exit as a truncation (`set-aside-journal`).
        unverified = (
            shown is None
            and not self._signed_by_absent_keys(root)
            and (
                (database is not None and database.mirrored)
                or self._disk.tombstone_chain_written(root.path)
            )
        )
        if unverified or judge(tags, database, sentinel):
            report.truncated_roots.add(root.root_id)
            return False
        report.chain_ok.add(root.root_id)
        if not write:
            return True
        tip = tip_of(tags)
        if not tags and database is None:
            return True  # no journal and none tracked: the first line starts the chain
        with _WRITE_LOCK:
            if (
                database is None
                or database.pending is not None
                or (database.seq, database.count, database.label) != (tip.seq, tip.count, tip.label)
            ):
                self._state.set_chain(root.root_id, tip)
            if sentinel is None or (sentinel.seq, sentinel.count, sentinel.label) != (
                tip.seq,
                tip.count,
                tip.label,
            ):
                self._mirror(root, tip)
            elif database is None or not database.mirrored:
                self._mirror(root, tip)  # the copy that could not be made then
        return True

    def has_pending(self, root_id: str) -> bool:
        """Lines still owed to this root (a purge ran while it was away), or a rewrite under a new
        key: fsops refuses every other write to it until a replay has written them (EF-BIB-052,
        GAP-12-14, EF-BIB-054 (10))."""
        if self._state.rekey_owed(root_id):
            return True
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(_pending.c.seq).where(_pending.c.root_id == root_id).limit(1)
                ).first()
                is not None
            )

    def flush_pending(self, root: TombstoneRoot) -> bool:
        """Write the lines a mounted root still owes (a transient error, or a root that was away),
        after the rewrite it owes if a `rekey` ran while it was away. True when nothing is owed
        any more. A root that is not mounted flushes nothing."""
        if root.mounted:
            self._rekey_if_owed(root)
            self._flush_pending(root)
        return not self.has_pending(root.root_id)

    def _flush_pending(self, root: TombstoneRoot) -> int:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(_pending.c.seq, _pending.c.line)
                .where(_pending.c.root_id == root.root_id)
                .order_by(_pending.c.seq)
            ).all()
        flushed = 0
        for seq, text in rows:
            line = text.encode("ascii")
            if not self._write_line(root, seq, parse_line(line[:-1]).kid, line):
                break
            flushed += 1
        return flushed

    # -- replay -----------------------------------------------------------------------------

    def _open(
        self, scope: str, kid: str, nonce: bytes, ct: bytes, root: str, seq: int
    ) -> tuple[TombstoneValues, dict[str, str]]:
        key = self._keyring.get(kid)
        plaintext = open_values(key.material, nonce, ct, scope=scope, root=root, seq=seq)
        return decode_plaintext(plaintext, scope)

    def _classify_unknown(
        self, root_id: str, scope: str, kid: str, seq: int, *, registered: set[str],
        lost: set[str], report: ReplayReport,
    ) -> None:  # fmt: skip
        if kid in lost:
            report.inert.append(InertLine(root_id, seq, "lost_keyring"))
        elif kid in registered:
            report.key_unknown.add((root_id, scope))
        else:
            report.inert.append(InertLine(root_id, seq, "kid_unregistered"))

    def replay(self, roots: Sequence[TombstoneRoot]) -> ReplayReport:
        """Rebuild the index from the table and every mounted journal; never deletes."""
        report = ReplayReport()
        for root in roots:
            if root.mounted:
                self._rekey_if_owed(root)  # before any other write to this root
                report.flushed += self._flush_pending(root)
        with self._engine.connect() as connection:
            db_kids = set(connection.execute(sa.select(_kid.c.kid)).scalars())
            lost = set(connection.execute(sa.select(_lost.c.kid)).scalars())
            rows = connection.execute(
                sa.select(
                    _tomb.c.scope, _tomb.c.seq, _tomb.c.kid, _tomb.c.nonce, _tomb.c.ct
                ).order_by(_tomb.c.seq)  # a lift takes out what came before it, in this order
            ).all()
        entries: list[tuple[TombstoneValues, dict[str, str]]] = []
        in_table: set[tuple[object, ...]] = set()
        for scope, seq, kid, nonce, ct in rows:
            if self._keyring.find(kid) is None:
                self._classify_unknown(DB_ROOT, scope, kid, seq, registered=db_kids, lost=lost,
                                       report=report)  # fmt: skip
                continue
            try:
                values, cited = self._open(scope, kid, bytes(nonce), bytes(ct), DB_ROOT, seq)
            except (EnvelopeError, TombstoneValueError):
                report.inert.append(InertLine(DB_ROOT, seq, "authentication_failed"))
                continue
            entries.append((values, cited))
            in_table.add(_identity(values, cited))
        heal: set[tuple[str, int]] = set()
        to_merge: list[tuple[TombstoneValues, dict[str, str]]] = []
        self._sentinel_kids = {}
        for root in roots:
            if root.mounted:
                self._replay_root(root, db_kids, lost, report, entries, in_table, to_merge, heal)
        report.verified = len(entries)
        for root_id in sorted(report.chain_ok):  # judged whole: nothing waits any more
            self._state.clear_truncated(root_id)
        for root_id in sorted(report.truncated_roots):
            if self._state.mark_truncated(root_id, announce=True):
                report.truncated_new.add(root_id)
        with self._engine.begin() as connection:
            for kid, seq in sorted(heal):
                self._register_kid(connection, kid, seq)
            for values, cited in to_merge:
                plaintext = encode_plaintext(values, cited)
                self._insert_row(connection, plaintext, values.scope, reason="replayed")
                report.merged += 1
            published = self._announce(connection, report)
        self._bus.deliver(published)
        index = MembershipIndex(self._normalizers)
        index.add_all(_not_lifted(entries))
        self._index, self._report = index, report
        return report

    def _replay_root(
        self,
        root: TombstoneRoot,
        db_kids: set[str],
        lost: set[str],
        report: ReplayReport,
        entries: list[tuple[TombstoneValues, dict[str, str]]],
        in_table: set[tuple[object, ...]],
        to_merge: list[tuple[TombstoneValues, dict[str, str]]],
        heal: set[tuple[str, int]],
    ) -> None:
        entries_on_disk = self._disk.tombstone_kids(root.path)
        sentinel = self._authenticated(root.root_id, entries_on_disk)
        self._sentinel_kids[root.root_id] = frozenset(sentinel)
        # An entry whose tag does not verify is not a registration (a third party that writes
        # the sentinel): an alert, never a block. Not reported for a kid the database knows.
        report.inert.extend(
            InertLine(root.root_id, None, "kid_unregistered")
            for kid in sorted(set(entries_on_disk) - sentinel - db_kids)
        )
        heal.update((kid, 1) for kid in sentinel - db_kids if self._keyring.find(kid) is not None)
        try:
            raw = self._disk.read_tombstone_journal(root.path, max_bytes=MAX_JOURNAL_BYTES)
            # an empty file is a journal that was created and never got its header (a crash in
            # between): as good as none, not a damaged one
            journal = None if not raw else parse_journal(raw)
        except (OSError, JournalFormatError):
            report.unreadable_roots.add(root.root_id)  # fail closed on this root
            return
        if journal is None:
            self._judge_chain(root, {}, report)  # a journal that is gone is a journal cut short
            return
        report.inert.extend(
            InertLine(root.root_id, None, "malformed", line=bad.number) for bad in journal.malformed
        )
        registered = db_kids | sentinel
        # the lines that open, and those of a key we lack: seq -> AEAD tag (the smallest, if many)
        tags: dict[int, bytes] = {}
        for line in journal.lines:
            if self._keyring.find(line.kid) is None:
                self._classify_unknown(root.root_id, line.scope, line.kid, line.seq,
                                       registered=registered, lost=lost, report=report)  # fmt: skip
                if line.kid in registered or line.kid in lost:
                    # a key we do not hold (missing or lost): the line cannot be opened, but its
                    # tag is the tag of the ciphertext, which needs no key. The chain stays
                    # verifiable after `accept-lost-keyring`.
                    tags[line.seq] = min(tag_of(line.ct), tags.get(line.seq, tag_of(line.ct)))
                continue
            opened = None
            for candidate in (root.root_id, *root.predecessor_root_ids):
                try:
                    opened = self._open(
                        line.scope, line.kid, line.nonce, line.ct, candidate, line.seq
                    )
                except (EnvelopeError, TombstoneValueError):
                    continue
                if candidate != root.root_id:
                    report.root_mismatch.add(root.root_id)
                break
            if opened is None:
                report.inert.append(InertLine(root.root_id, line.seq, "authentication_failed"))
                continue
            if line.kid not in db_kids:
                heal.add((line.kid, line.seq))
            tag = tag_of(line.ct)
            tags[line.seq] = min(tag, tags.get(line.seq, tag))
            identity_ = _identity(*opened)
            if identity_ not in in_table:
                in_table.add(identity_)
                entries.append(opened)
                to_merge.append(opened)
        self._judge_chain(root, tags, report)

    def scan_chains(self, roots: Sequence[TombstoneRoot]) -> set[str]:
        """Judge the journals now, read only (`status` runs it: a cut made while the server runs
        is seen without a restart): the waiting marks follow, nothing is logged or published (the
        replay sends the alert, once). The roots found truncated."""
        report = ReplayReport()
        with self._engine.connect() as connection:
            db_kids = set(connection.execute(sa.select(_kid.c.kid)).scalars())
            lost = set(connection.execute(sa.select(_lost.c.kid)).scalars())
        for root in roots:
            if not root.mounted:
                continue
            try:
                raw = self._disk.read_tombstone_journal(root.path, max_bytes=MAX_JOURNAL_BYTES)
                journal = None if not raw else parse_journal(raw)
            except (OSError, JournalFormatError):
                continue  # unreadable: `set-aside-journal` is its exit, not this one's
            tags: dict[int, bytes] = {}
            registered = db_kids | self._authenticated(
                root.root_id, self._disk.tombstone_kids(root.path)
            )
            for line in journal.lines if journal is not None else ():
                if self._keyring.find(line.kid) is None:
                    if line.kid in registered | lost:
                        tags[line.seq] = min(tag_of(line.ct), tags.get(line.seq, tag_of(line.ct)))
                    continue
                for candidate in (root.root_id, *root.predecessor_root_ids):
                    try:
                        self._open(line.scope, line.kid, line.nonce, line.ct, candidate, line.seq)
                    except (EnvelopeError, TombstoneValueError):
                        continue
                    tags[line.seq] = min(tag_of(line.ct), tags.get(line.seq, tag_of(line.ct)))
                    break
            self._judge_chain(root, tags, report, write=False)
        for root_id in sorted(report.chain_ok):
            self._state.clear_truncated(root_id)
        for root_id in sorted(report.truncated_roots):
            self._state.mark_truncated(root_id, announce=False)  # the replay tells, once
        return report.truncated_roots

    def _announce(self, connection: Connection, report: ReplayReport) -> list[PublishedEvent]:
        published: list[PublishedEvent] = []

        def root_payload(root_id: str) -> dict[str, object]:
            return {} if root_id == DB_ROOT else {"root_id": root_id}

        # One alert per (root, seq, reason): a replay at every start must not repeat it.
        def unverified_key(payload: Mapping[str, object]) -> tuple[object, ...]:
            return tuple(payload.get(k) for k in ("root_id", "seq", "reason", "line"))

        seen = {
            unverified_key(dict(row))
            for row in connection.execute(
                sa.select(platform.event.c.payload).where(
                    platform.event.c.type == "tombstone.unverified"
                )
            ).scalars()
        }
        for line in report.inert:
            payload: dict[str, object] = {
                **root_payload(line.root_id), "reason": line.reason, "seq": line.seq,
            }  # fmt: skip
            if line.line is not None:
                payload["line"] = line.line
            if unverified_key(payload) in seen:
                continue
            seen.add(unverified_key(payload))
            published.append(self._bus.publish(connection, "tombstone.unverified", payload))
        for root_id, scope in sorted(report.key_unknown):
            payload = {**root_payload(root_id), "scope": scope}
            published.append(self._bus.publish(connection, "tombstone.key_unknown", payload))
        for root_id in sorted(report.root_mismatch):
            published.append(
                self._bus.publish(connection, "tombstone.root_mismatch", {"root_id": root_id})
            )
        for root_id in sorted(report.truncated_new):
            published.append(
                self._bus.publish(connection, "tombstone.journal_truncated", {"root_id": root_id})
            )
        return published

    # -- membership -------------------------------------------------------------------------

    def check(
        self, candidate: TombstoneValues, *, root_id: str | None, hash_with: HashWith | None = None
    ) -> Verdict:
        """May this candidate be (re)acquired onto `root_id` (None: no root known yet)?"""
        if self._index is None:
            raise NotReplayedError("replay the tombstones before any reacquisition")
        report = self._report
        # A root's state blocks reacquisition onto that root only; with no root known yet, only
        # the database's own rows count (the root is checked again when the destination is).
        if root_id is not None and root_id in report.unreadable_roots:
            return Verdict(True, "journal_unreadable")
        if root_id is not None and self._truncated(root_id):
            return Verdict(True, "journal_truncated")
        for where, scope in report.key_unknown:
            if scope == candidate.scope and where in (DB_ROOT, root_id):
                return Verdict(True, "key_unknown")
        if isinstance(candidate, (SeriesBindingRef, VariantRef)):
            return self._check_reference(candidate, hash_with)
        if self._index.contains(candidate, hash_with=hash_with):
            return Verdict(True, "tombstoned")
        return Verdict(False)

    # -- the one place an upstream reference meets the tombstones (EF-SRC-022, EF-BIB-054 (4)) --

    def _alias_edges(self, plugin_id: str) -> list[tuple[str, str, str, str]]:
        """`source_alias` of the plugin, every scheme. Cached by the table's own signature (count
        and last id: an alias is only ever added), so a check costs one small query."""
        table = acquisition.source_alias
        with self._engine.connect() as connection:
            signature = tuple(
                connection.execute(sa.select(sa.func.count(), sa.func.max(table.c.id))).one()
            )
            if signature != self._alias_signature:
                rows = connection.execute(
                    sa.select(table.c.plugin_id, table.c.old_source_key, table.c.old_remote_id,
                              table.c.new_source_key, table.c.new_remote_id)
                )  # fmt: skip
                grouped: dict[str, list[tuple[str, str, str, str]]] = {}
                for plugin, old_key, old_id, new_key, new_id in rows:
                    grouped.setdefault(plugin, []).append((old_key, old_id, new_key, new_id))
                self._alias_cache, self._alias_signature = grouped, signature
        return self._alias_cache.get(plugin_id, [])

    def _reference_family(
        self, candidate: SeriesBindingRef | VariantRef
    ) -> list[SeriesBindingRef | VariantRef] | None:
        """The candidate and every other form it has: for a series, each reference of its
        connected component in the alias graph (every scheme, both directions, cycle-safe); for a
        Unit, the same remote id under each source key the aliases tie to its own. None when the
        graph is too large to follow: the caller blocks."""
        edges = self._alias_edges(candidate.source_key)  # `source_key` holds the plugin id
        if isinstance(candidate, SeriesBindingRef):
            nodes = alias_family(edges, (candidate.sub_source, candidate.series_ref))
            if nodes is None:
                return None
            return [SeriesBindingRef(candidate.source_key, key, ref) for key, ref in nodes]
        keys = alias_family(
            [(ok, "", nk, "") for ok, _oi, nk, _ni in edges], (candidate.sub_source, "")
        )
        if keys is None:
            return None
        return [VariantRef(candidate.source_key, key, candidate.remote_unit_id) for key, _ in keys]

    def _check_reference(
        self, candidate: SeriesBindingRef | VariantRef, hash_with: HashWith | None
    ) -> Verdict:
        """Every door asks this (`check`): the reference as received, its spellings and its other
        forms through the aliases. A form that meets a tombstone, whatever the way it got here
        (an alias, a case, a chain, another scheme, `migrate_ref`), is the erased reference."""
        if self._index is None:
            raise NotReplayedError("replay the tombstones before any reacquisition")
        try:
            for part in (candidate.sub_source, candidate.source_key):
                fold_ref(part)
            fold_ref(
                candidate.series_ref if isinstance(candidate, SeriesBindingRef)
                else candidate.remote_unit_id
            )  # fmt: skip
        except EmptyRefError:
            return Verdict(True, "empty_ref")  # names nothing: refused, never compared
        if self._index.contains(candidate, hash_with=hash_with):
            return Verdict(True, "tombstoned")
        family = self._reference_family(candidate)
        if family is None:
            return Verdict(True, "alias_graph_too_large")
        if any(self._index.contains_folded(form) for form in [candidate, *family]):
            return Verdict(True, "tombstoned")
        return Verdict(False)

    def has_values(self, candidate: TombstoneValues) -> bool:
        """These very values are already tombstoned (a purge run twice records them once). Unlike
        `check`, a shared content hash does not count: another Variant of the same file is not
        covered until its own reference is."""
        if self._index is None:
            raise NotReplayedError("replay the tombstones before any reacquisition")
        return self._index.contains_values(candidate)

    def check_content(
        self, hashes: Iterable[ContentHash] = (), *, hash_with: HashWith | None = None
    ) -> bool:
        """A file without an upstream reference (import, adoption): its content alone."""
        if self._index is None:
            raise NotReplayedError("replay the tombstones before any reacquisition")
        return self._index.contains_content(hashes, hash_with=hash_with)

    def reconcile_allowed(self, root_id: str) -> bool:
        """`reconcile --adopt-after-restore` on this root: refused while a registered kid is
        missing from the keyring (EF-BIB-053, passes 12 and 13) or the journal is unreadable."""
        if self._index is None:
            return False
        report = self._report
        if root_id in report.unreadable_roots or self._truncated(root_id):
            return False
        return not any(where in (DB_ROOT, root_id) for where, _ in report.key_unknown)

    def _truncated(self, root_id: str) -> bool:
        """The journal of this root is short of its chain: found by a replay of this process, or
        recorded by another (`status`, a restart) in the waiting mark."""
        return root_id in self._report.truncated_roots or self._state.is_truncated(root_id)

    def screen_migration(
        self, rows: Iterable[tuple[str, TombstoneValues]], *, root_id: str | None = None
    ) -> set[str]:
        """`tombstone_check` of an identity migration (EF-BIB-054 (4)): the row ids that, once
        migrated (or under their reverse alias), would match a tombstone. They are blocked and
        `tombstone.migration_hit` is emitted once per scope hit."""
        blocked: set[str] = set()
        scopes: set[Scope] = set()
        for row_id, candidate in rows:
            if self.check(candidate, root_id=root_id).blocked:
                blocked.add(row_id)
                scopes.add(candidate.scope)
        self._tell_hits(scopes)
        return blocked

    def check_refs(
        self, refs: Iterable[SeriesBindingRef | VariantRef], *, root_id: str | None = None
    ) -> Verdict:
        """Blocked when any of the references is (`check` already follows aliases and spellings)."""
        for ref in refs:
            verdict = self.check(ref, root_id=root_id)
            if verdict.blocked:
                return verdict
        return Verdict(False)

    def erased_series_refs(self, plugin_id: str, source_key: str) -> list[str]:
        """The series of this source that were erased (the references as received), for a migration
        that must learn their new form so that it stays erased. Read in memory, never written."""
        if self._index is None:
            raise NotReplayedError("replay the tombstones before any migration")
        return self._index.series_refs(plugin_id, source_key)

    def erased_series_plugins(self, source_key: str) -> list[str]:
        """The plugins that have an erased series on this source (read in memory, never written)."""
        if self._index is None:
            raise NotReplayedError("replay the tombstones before any migration")
        return self._index.series_plugins(source_key)

    def screen_refs(
        self,
        rows: Iterable[tuple[str, Sequence[SeriesBindingRef | VariantRef]]],
        *,
        announce: bool = True,
        root_id: str | None = None,
    ) -> set[str]:
        """`screen_migration` for alias families: the row ids with a tombstoned member. With
        `announce=False` nothing is written (a preview, or a caller that holds the write lock
        and tells the hits itself with `announce_hits`)."""
        blocked: set[str] = set()
        scopes: set[Scope] = set()
        for row_id, family in rows:
            verdict = self.check_refs(family, root_id=root_id)
            if verdict.blocked:
                blocked.add(row_id)
                scopes.update(ref.scope for ref in family)
        if announce:
            self._tell_hits(scopes)
        return blocked

    def announce_hits(self, scopes: Iterable[Scope]) -> None:
        """Tell the hits of a migration that was applied, once per scope (the scope only)."""
        self._tell_hits(set(scopes))

    def _tell_hits(self, scopes: set[Scope]) -> None:
        if not scopes:
            return
        with self._engine.begin() as connection:
            published = [
                self._bus.publish(connection, "tombstone.migration_hit", {"scope": scope})
                for scope in sorted(scopes)
            ]
        self._bus.deliver(published)

    # -- the "lost keyring" waiver (R62) ----------------------------------------------------

    def owed_roots(self) -> list[str]:
        """The roots that still owe tombstone lines (a purge ran while they were away)."""
        with self._engine.connect() as connection:
            owed = set(connection.execute(sa.select(_pending.c.root_id)).scalars())
        return sorted(owed | self._state.rekey_owed_roots())

    def set_aside_journal(
        self,
        roots: Sequence[TombstoneRoot],
        *,
        principal: Principal,
        now: datetime,
        root_id: str | None = None,
    ) -> dict[str, str]:
        """Reserved action: a journal that cannot be read (a truncated header, a newer major
        format), or that was cut short of its chain (`tombstone_journal_truncated`, D12), is put
        aside under a time-stamped name, never deleted. Before that, the file is
        read again ignoring its header: every line that opens with a key of the keyring (AEAD,
        known `kid`, this root or a predecessor) and is not in the table yet is merged into the
        table, so a purge that only this journal knew (the database being older, after a restore)
        stays in force. A line whose key is registered but absent from the keyring cannot be
        checked: the action is refused, naming the way out (restore the keyring, or accept its
        loss). The root's journal is then written again from the table, every row sealed for
        that root and owed to it, and its chained label starts again with the first line; the
        sentinel's registry of kids is not rebuilt here, it is completed when the first line is
        written. A truncated journal that is gone altogether has nothing to put aside, and is
        written again all the same. Returns `root id -> aside name`."""
        self._authorize(principal, _SET_ASIDE_ACTION, now)
        self.replay(roots)
        waiting = self._report.unreadable_roots | self._report.truncated_roots
        chosen = [
            r
            for r in roots
            if r.mounted and r.root_id in waiting and (root_id is None or r.root_id == root_id)
        ]
        found: dict[str, list[tuple[TombstoneValues, dict[str, str]]]] = {}
        for root in chosen:  # all checked before the first one is touched
            found[root.root_id] = self._authentic_lines(root)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        done: dict[str, str] = {}
        for root in chosen:
            with _WRITE_LOCK:
                # The database goes first, with the audit, in one transaction: a kill before the
                # `rename` leaves the old file in place and every line owed to it (appended
                # again, harmlessly: a repeated line is one line); the other order could lose, in
                # the set-aside file, a line that only this journal held.
                with self._engine.begin() as connection:
                    self._merge_into_table(found[root.root_id], connection)
                    self._owe_every_line(root.root_id, connection)
                    # the new journal starts a new chain, rewritten from the table under the
                    # active key
                    self._state.set_chain(root.root_id, None, connection)
                    self._state.clear_rekey_owed(root.root_id, connection)
                    connection.execute(
                        sa.insert(identity.audit).values(
                            id=new_ulid(),
                            actor_id=principal.actor_id,
                            action="tombstone.set_aside_journal",
                            target=f"root:{root.root_id}",
                            session_id=principal.session_id,
                            auth_time=principal.auth_time,
                        )
                    )
                aside = self._disk.set_aside_tombstone_journal(root.path, stamp)
            if aside is not None:
                done[root.root_id] = aside
        self.replay(roots)  # the lines owed are written when the root accepts writes
        return done

    def _authentic_lines(self, root: TombstoneRoot) -> list[tuple[TombstoneValues, dict[str, str]]]:
        """The lines of the damaged journal that open under a key we hold. A line that does not
        authenticate is inert (EF-BIB-054) and left in the file; one whose key is registered but
        missing refuses the whole action."""
        try:
            raw = self._disk.read_tombstone_journal(root.path, max_bytes=MAX_JOURNAL_BYTES)
            lines = parse_lines_ignoring_header(raw or b"")
        except (OSError, JournalFormatError):
            lines = []
        with self._engine.connect() as connection:
            registered = set(connection.execute(sa.select(_kid.c.kid)).scalars())
            lost = set(connection.execute(sa.select(_lost.c.kid)).scalars())
        registered |= self._authenticated(root.root_id, self._disk.tombstone_kids(root.path))
        opened: list[tuple[TombstoneValues, dict[str, str]]] = []
        for line in lines:
            if self._keyring.find(line.kid) is None:
                if line.kid in registered and line.kid not in lost:
                    raise SetAsideRefusedError(
                        f"the journal of root {root.root_id} holds lines sealed with a key that "
                        "the keyring lacks: restore the keyring from its export, or accept its "
                        "loss with `shelfhold tombstone accept-lost-keyring`, then try again"
                    )
                continue
            for candidate in (root.root_id, *root.predecessor_root_ids):
                try:
                    opened.append(
                        self._open(line.scope, line.kid, line.nonce, line.ct, candidate, line.seq)
                    )
                except (EnvelopeError, TombstoneValueError):
                    continue
                break
        return opened

    def _merge_into_table(
        self, entries: list[tuple[TombstoneValues, dict[str, str]]], connection: Connection
    ) -> None:
        """Add to the table the authentic lines it does not hold (never a deletion), in the
        transaction of `connection`."""
        rows = connection.execute(
            sa.select(_tomb.c.scope, _tomb.c.seq, _tomb.c.kid, _tomb.c.nonce, _tomb.c.ct)
        ).all()
        known: set[tuple[object, ...]] = set()
        for scope, seq, kid, nonce, ct in rows:
            if self._keyring.find(kid) is None:
                continue
            try:
                known.add(_identity(*self._open(scope, kid, bytes(nonce), bytes(ct), DB_ROOT, seq)))
            except (EnvelopeError, TombstoneValueError):
                continue
        for values, cited in entries:
            if _identity(values, cited) in known:
                continue
            known.add(_identity(values, cited))
            self._insert_row(
                connection, encode_plaintext(values, cited), values.scope, reason="replayed"
            )

    def _owe_every_line(self, root_id: str, connection: Connection) -> None:
        """Seal every row of the table for this root and keep it as a line owed to it (in the
        transaction of `connection`). A row whose key is not in the keyring cannot be sealed
        again: it stays in the set-aside file."""
        rows = connection.execute(
            sa.select(_tomb.c.scope, _tomb.c.seq, _tomb.c.kid, _tomb.c.nonce, _tomb.c.ct).order_by(
                _tomb.c.seq
            )
        ).all()
        for scope, seq, kid, nonce, ct in rows:
            if self._keyring.find(kid) is None:
                continue
            try:
                values, cited = self._open(scope, kid, bytes(nonce), bytes(ct), DB_ROOT, seq)
            except (EnvelopeError, TombstoneValueError):
                continue
            line = self._line(encode_plaintext(values, cited), scope, root_id, seq)
            connection.execute(
                sa.insert(_pending)
                .values(root_id=root_id, seq=seq, line=line.decode("ascii"))
                .prefix_with("OR IGNORE")
            )

    def owe_lines(self, root_id: str) -> None:
        """Every row of the table becomes a line owed to this root (a root stamped again after a
        takeover or a reinit has a journal that lacks them): they are written by the next flush.
        Its chained label is taken again from the journal as it will be."""
        with _WRITE_LOCK, self._engine.begin() as connection:
            self._owe_every_line(root_id, connection)
            self._state.set_chain(root_id, None, connection)

    # -- `tombstone rekey` (EF-BIB-054 (10)) ------------------------------------------------

    def _compromised(self) -> set[str]:
        return {key.kid for key in self._keyring.keys if key.state == "compromised"}

    def rekey(
        self, roots: Sequence[TombstoneRoot], *, principal: Principal, now: datetime
    ) -> RekeyReport:
        """Reserved action, after a key leak: every table row, every line owed to a root and every
        journal line sealed under a key marked `compromised` is sealed again under the active key
        (the keyring says which: `rotation.mark_compromised`, after a rotation). The old key stays
        in the keyring and the registry, to read old backups, and `status` warns of it.

        The database goes first, in one transaction that also writes the rewrite each root owes:
        from then on a kill, a root that is away, a root that fails, all end the same way, the
        journal is rewritten when the root is replayed, before any other write to it. A mounted
        root is rewritten here: a new file, `fsync`, one atomic `rename`; the file is the old one
        or the new one, never half of either. A root whose journal is unreadable or truncated is
        left as it is and still owes the rewrite: `tombstone set-aside-journal` is its way out."""
        self._authorize(principal, _REKEY_ACTION, now)
        compromised = self._compromised()
        if not compromised:
            raise ValueError("no key is marked compromised: nothing to rekey")
        table_rows = self._rekey_database(compromised, roots)
        rewritten: dict[str, int] = {}
        for root in roots:
            if root.mounted:
                count = self._rekey_if_owed(root, now=now)
                if count is not None:
                    rewritten[root.root_id] = count
        owed = tuple(r.root_id for r in roots if self._state.rekey_owed(r.root_id))
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(),
                    actor_id=principal.actor_id,
                    action="tombstone.rekey",
                    target="kids:" + ",".join(sorted(compromised)),
                    session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
        log.info("tombstone.rekeyed", table_rows=table_rows, roots=len(rewritten), owed=len(owed))
        return RekeyReport(rewritten, owed, table_rows)

    def _rekey_database(self, compromised: set[str], roots: Sequence[TombstoneRoot]) -> int:
        """The table and the lines owed, sealed again; every root marked as owing its journal."""
        key = self._keyring.active
        resealed = 0
        with _WRITE_LOCK, self._engine.begin() as connection:
            key_registry.register_active_key(connection, self._keyring)
            self._register_kid(connection, key.kid, self._next_seq(connection))
            rows = connection.execute(
                sa.select(_tomb.c.id, _tomb.c.scope, _tomb.c.seq, _tomb.c.kid, _tomb.c.nonce,
                          _tomb.c.ct).where(_tomb.c.kid.in_(compromised))
            ).all()  # fmt: skip
            for row_id, scope, seq, kid, nonce, ct in rows:
                if self._keyring.find(kid) is None:
                    continue
                try:
                    values, cited = self._open(scope, kid, bytes(nonce), bytes(ct), DB_ROOT, seq)
                except (EnvelopeError, TombstoneValueError):
                    continue  # inert (it never opened): left as it is
                new_nonce, new_ct = seal_values(
                    key.material, encode_plaintext(values, cited), scope=scope, root=DB_ROOT,
                    seq=seq,
                )  # fmt: skip
                connection.execute(
                    sa.update(_tomb).where(_tomb.c.id == row_id)
                    .values(kid=key.kid, nonce=new_nonce, ct=new_ct)
                )  # fmt: skip
                resealed += 1
            for root_id, seq, text in connection.execute(
                sa.select(_pending.c.root_id, _pending.c.seq, _pending.c.line)
            ).all():
                owed_line = parse_line(text.encode("ascii")[:-1])
                if owed_line.kid not in compromised or self._keyring.find(owed_line.kid) is None:
                    continue
                try:
                    values, cited = self._open(
                        owed_line.scope,
                        owed_line.kid,
                        owed_line.nonce,
                        owed_line.ct,
                        root_id,
                        owed_line.seq,
                    )
                except (EnvelopeError, TombstoneValueError):
                    continue
                fresh = self._line(encode_plaintext(values, cited), owed_line.scope, root_id, seq)
                connection.execute(
                    sa.update(_pending)
                    .where(_pending.c.root_id == root_id, _pending.c.seq == seq)
                    .values(line=fresh.decode("ascii"))
                )
                resealed += 1
            for root in roots:  # until its journal says otherwise, in this very transaction
                self._state.owe_rekey(root.root_id, connection)
        return resealed

    def _rekey_if_owed(self, root: TombstoneRoot, *, now: datetime | None = None) -> int | None:
        """Rewrite the journal of a mounted root that owes it. The lines resealed, or None when
        the journal cannot be rewritten (unreadable, truncated, keys missing): the root keeps
        owing it. Nothing owed: 0."""
        if not self._state.rekey_owed(root.root_id):
            return 0
        compromised = self._compromised()
        if not compromised:
            self._state.clear_rekey_owed(root.root_id)  # nothing is compromised any more
            return 0
        with _WRITE_LOCK:
            count = self._rekey_journal(root, compromised, now or datetime.now().astimezone())
            if count is not None:
                self._state.clear_rekey_owed(root.root_id)
            return count

    def _reseal_row(
        self, row: bytes, root: TombstoneRoot, compromised: set[str]
    ) -> tuple[bytes, JournalLine | None, JournalLine | None]:
        """A row of the journal as it will be written, with the line it was and the line it is
        when that line opens (else None for both: the row is kept verbatim)."""
        try:
            line = parse_line(row)
        except (UnicodeDecodeError, ValueError):
            return row, None, None
        if self._keyring.find(line.kid) is None:
            return row, None, None
        opened = None
        for candidate in (root.root_id, *root.predecessor_root_ids):
            try:
                opened = self._open(line.scope, line.kid, line.nonce, line.ct, candidate, line.seq)
            except (EnvelopeError, TombstoneValueError):
                continue
            break
        if opened is None:
            return row, None, None  # inert: it stays, nothing is lost
        if line.kid not in compromised:
            return row, line, line
        fresh = self._line(encode_plaintext(*opened), line.scope, root.root_id, line.seq)
        return fresh[:-1], line, parse_line(fresh[:-1])

    def _rekey_journal(
        self, root: TombstoneRoot, compromised: set[str], now: datetime
    ) -> int | None:
        for _attempt in range(_REWRITE_ATTEMPTS):
            try:
                raw = self._disk.read_tombstone_journal(root.path, max_bytes=MAX_JOURNAL_BYTES)
                if not raw:
                    return 0
                parse_journal(raw)  # a journal that cannot be read is not rewritten
            except (OSError, JournalFormatError):
                return None
            rows = raw.split(b"\n")
            if rows[-1] == b"":
                rows.pop()
            out = [rows[0]]
            before: dict[int, bytes] = {}
            after: dict[int, bytes] = {}
            count = 0
            for row in rows[1:]:
                if not row:
                    continue
                kept, old, new = self._reseal_row(row, root, compromised)
                out.append(kept)
                if old is None or new is None:
                    held = self._held_tag(row)  # a key we lack: copied as it is, tag unchanged
                    if held is not None:
                        before[held[0]] = min(held[1], before.get(held[0], held[1]))
                        after[held[0]] = min(held[1], after.get(held[0], held[1]))
                    continue
                before[old.seq] = min(tag_of(old.ct), before.get(old.seq, tag_of(old.ct)))
                after[new.seq] = min(tag_of(new.ct), after.get(new.seq, tag_of(new.ct)))
                count += new is not old
            report = ReplayReport()
            if not self._judge_chain(root, before, report, write=False):
                return None  # truncated: rewriting it would make the cut permanent
            if count == 0:
                return 0
            new_bytes = b"\n".join(out) + b"\n"
            old_tip, new_tip = tip_of(before), tip_of(after)
            self._state.set_chain(
                root.root_id,
                ChainState(old_tip.seq, old_tip.count, old_tip.label, pending=new_tip.label),
            )
            local = root.path.rstrip("/")
            intent = self._state.record_intent(
                src=f"{local}/{REWRITE_PATH}", dst=f"{local}/{JOURNAL_PATH}",
                sha256=hashlib.sha256(new_bytes).hexdigest(),
            )  # fmt: skip
            try:
                replaced = self._disk.replace_tombstone_journal(root.path, new_bytes, raw)
            except BaseException:
                self._state.close_intent(intent, "cancelled", now)
                raise
            if not replaced:
                self._state.close_intent(intent, "cancelled", now)
                continue  # written meanwhile: read it again
            self._state.set_chain(root.root_id, new_tip)
            self._mirror(root, new_tip)
            self._state.close_intent(intent, "done", now)
            return count
        return None

    def _held_tag(self, row: bytes) -> tuple[int, bytes] | None:
        """`(seq, tag)` of a line that opens under no key we hold, of a kid the registries know
        (lost or missing): it counts in the chain by its tag, which needs no key."""
        try:
            line = parse_line(row)
        except (UnicodeDecodeError, ValueError):
            return None
        if self._keyring.find(line.kid) is not None:
            return None
        with self._engine.connect() as connection:
            known = set(connection.execute(sa.select(_kid.c.kid)).scalars())
            known |= set(connection.execute(sa.select(_lost.c.kid)).scalars())
        return (line.seq, tag_of(line.ct)) if line.kid in known else None

    def accept_lost_keyring(
        self,
        roots: Sequence[TombstoneRoot],
        *,
        confirmation: str,
        principal: Principal,
        now: datetime,
        frozen: bool = False,
    ) -> list[str]:
        """Reserved action (checked here too, not only at the route), typed confirmation,
        audited with its actor and the kids: the registered kids missing from the keyring
        become "lost", and their lines inert. Only kids of the database's registry or of an
        authenticated sentinel entry are waived. Returns the kids waived."""
        self._authorize(principal, _LOST_KEYRING_ACTION, now)
        actor_id = principal.actor_id
        phrase = FROZEN_LOST_KEYRING_PHRASE if frozen else LOST_KEYRING_PHRASE
        if confirmation != phrase:
            raise LostKeyringConfirmationError(
                f"type the confirmation phrase exactly: {phrase}" if frozen
                else "type the confirmation phrase exactly"
            )  # fmt: skip
        self.replay(roots)
        with self._engine.begin() as connection:
            db_kids = set(connection.execute(sa.select(_kid.c.kid)).scalars())
            lost = set(connection.execute(sa.select(_lost.c.kid)).scalars())
            seen = set().union(*self._sentinel_kids.values()) if self._sentinel_kids else set()
            missing = sorted(
                kid for kid in (db_kids | seen) - lost if self._keyring.find(kid) is None
            )
            for kid in missing:
                if kid not in db_kids:
                    self._register_kid(connection, kid, 1)
                connection.execute(sa.insert(_lost).values(kid=kid, actor_id=actor_id))
            connection.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(),
                    actor_id=actor_id,
                    action="tombstone.accept_lost_keyring",
                    target="kids:" + ",".join(missing),
                    session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
        self.replay(roots)
        return missing
