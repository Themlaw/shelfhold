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

**Membership** (`check`): against the in-memory index only; it fails closed until a replay ran.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, identity, platform
from shelfhold.infrastructure.secrets import registry as key_registry
from shelfhold.infrastructure.secrets.errors import EnvelopeError
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.tombstone_aead import (
    ALG,
    DB_ROOT,
    kid_tag,
    open_values,
    seal_values,
    verify_kid_tag,
)
from shelfhold.library.tombstones.cited import default_normalizers
from shelfhold.library.tombstones.index import (
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
)
from shelfhold.library.tombstones.values import (
    ContentHash,
    Scope,
    TombstoneValueError,
    TombstoneValues,
    decode_plaintext,
    encode_plaintext,
)

TABLE_FORMAT: Final = 2  # `content_tombstone.v`: format 2.0, the same associated data as a line
LOST_KEYRING_PHRASE: Final = "the keyring is lost"
INERT_REASONS: Final = frozenset(
    {"authentication_failed", "kid_unregistered", "lost_keyring", "malformed"}
)
# `content_tombstone.reason`: a closed list, never free text (it could carry a title, I36).
REASONS: Final = frozenset({"purge_unit", "purge_work", "replayed"})
_LOST_KEYRING_ACTION = RESERVED_ACTIONS["tombstone.accept_lost_keyring"]
_SET_ASIDE_ACTION = RESERVED_ACTIONS["tombstone.set_aside_journal"]

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

    def append_tombstone_line(self, root_path: str, line: bytes, *, header: bytes) -> None: ...

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


@dataclass(frozen=True)
class Verdict:
    blocked: bool
    cause: str | None = None  # tombstoned | key_unknown | journal_unreadable


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
        self._report = ReplayReport()
        self._sentinel_kids: dict[str, frozenset[str]] = {}  # authenticated entries only

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
        if reason is not None and reason not in REASONS:
            raise ValueError(f"reason is one of {sorted(REASONS)}")
        cited = self.cited_now(values)
        plaintext = encode_plaintext(values, cited)
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
        if self._index is not None:  # the row is committed: it blocks now, whatever the disk does
            self._index.add_all([(values, cited)])
        written: list[str] = []
        for root in roots:
            if root.mounted and self._write_line(root, seq, kid, lines[root.root_id]):
                written.append(root.root_id)
        pending = tuple(r.root_id for r in roots if r.root_id not in written)
        return Recorded(seq, kid, tuple(written), pending)

    def _write_line(self, root: TombstoneRoot, seq: int, kid: str, line: bytes) -> bool:
        """Registry of the sentinel first, then the line, then the pending row goes."""
        try:
            self._disk.record_tombstone_kid(root.path, kid, self._kid_tags(root.root_id, kid))
            self._disk.append_tombstone_line(root.path, line, header=header_bytes())
        except (OSError, ValueError, RuntimeError):
            return False  # stays pending: written at the next replay of this root
        with self._engine.begin() as connection:
            connection.execute(
                sa.delete(_pending).where(_pending.c.root_id == root.root_id, _pending.c.seq == seq)
            )
        return True

    def has_pending(self, root_id: str) -> bool:
        """Lines still owed to this root (a purge ran while it was away): fsops refuses every
        other write to it until a replay has written them (EF-BIB-052, GAP-12-14)."""
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(_pending.c.seq).where(_pending.c.root_id == root_id).limit(1)
                ).first()
                is not None
            )

    def flush_pending(self, root: TombstoneRoot) -> bool:
        """Write the lines a mounted root still owes (a transient error, or a root that was away).
        True when nothing is owed any more. A root that is not mounted flushes nothing."""
        if root.mounted:
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
                report.flushed += self._flush_pending(root)
        with self._engine.connect() as connection:
            db_kids = set(connection.execute(sa.select(_kid.c.kid)).scalars())
            lost = set(connection.execute(sa.select(_lost.c.kid)).scalars())
            rows = connection.execute(
                sa.select(_tomb.c.scope, _tomb.c.seq, _tomb.c.kid, _tomb.c.nonce, _tomb.c.ct)
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
        index.add_all(entries)
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
            return
        report.inert.extend(
            InertLine(root.root_id, None, "malformed", line=bad.number) for bad in journal.malformed
        )
        registered = db_kids | sentinel
        for line in journal.lines:
            if self._keyring.find(line.kid) is None:
                self._classify_unknown(root.root_id, line.scope, line.kid, line.seq,
                                       registered=registered, lost=lost, report=report)  # fmt: skip
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
            identity_ = _identity(*opened)
            if identity_ not in in_table:
                in_table.add(identity_)
                entries.append(opened)
                to_merge.append(opened)

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
        for where, scope in report.key_unknown:
            if scope == candidate.scope and where in (DB_ROOT, root_id):
                return Verdict(True, "key_unknown")
        if self._index.contains(candidate, hash_with=hash_with):
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
        if root_id in report.unreadable_roots:
            return False
        return not any(where in (DB_ROOT, root_id) for where, _ in report.key_unknown)

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
        if scopes:
            with self._engine.begin() as connection:
                published = [
                    self._bus.publish(connection, "tombstone.migration_hit", {"scope": scope})
                    for scope in sorted(scopes)
                ]
            self._bus.deliver(published)
        return blocked

    # -- the "lost keyring" waiver (R62) ----------------------------------------------------

    def owed_roots(self) -> list[str]:
        """The roots that still owe tombstone lines (a purge ran while they were away)."""
        with self._engine.connect() as connection:
            return sorted(set(connection.execute(sa.select(_pending.c.root_id)).scalars()))

    def set_aside_journal(
        self,
        roots: Sequence[TombstoneRoot],
        *,
        principal: Principal,
        now: datetime,
        root_id: str | None = None,
    ) -> dict[str, str]:
        """Reserved action: a journal that cannot be read (a truncated header, a damaged file)
        is put aside under a time-stamped name, never deleted, and the root's journal is written
        again from the database (every row sealed for that root) and the sentinels. This is the
        way out of a root that `journal_unreadable` blocks. Returns `root id -> aside name`."""
        verdict = authorize_reserved(principal, _SET_ASIDE_ACTION, now=now)
        if not isinstance(verdict, Allowed):
            raise LostKeyringRefusedError(verdict)
        self.replay(roots)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        done: dict[str, str] = {}
        for root in roots:
            if not root.mounted or root.root_id not in self._report.unreadable_roots:
                continue
            if root_id is not None and root.root_id != root_id:
                continue
            aside = self._disk.set_aside_tombstone_journal(root.path, stamp)
            if aside is None:
                continue
            self._owe_every_line(root.root_id)
            done[root.root_id] = aside
            with self._engine.begin() as connection:
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
        self.replay(roots)  # the lines owed are written when the root accepts writes
        return done

    def _owe_every_line(self, root_id: str) -> None:
        """Seal every row of the table for this root and keep it as a line owed to it. A row
        whose key is not in the keyring cannot be sealed again: it stays in the set-aside file."""
        with self._engine.begin() as connection:
            rows = connection.execute(
                sa.select(
                    _tomb.c.scope, _tomb.c.seq, _tomb.c.kid, _tomb.c.nonce, _tomb.c.ct
                ).order_by(_tomb.c.seq)
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

    def accept_lost_keyring(
        self,
        roots: Sequence[TombstoneRoot],
        *,
        confirmation: str,
        principal: Principal,
        now: datetime,
    ) -> list[str]:
        """Reserved action (checked here too, not only at the route), typed confirmation,
        audited with its actor and the kids: the registered kids missing from the keyring
        become "lost", and their lines inert. Only kids of the database's registry or of an
        authenticated sentinel entry are waived. Returns the kids waived."""
        verdict = authorize_reserved(principal, _LOST_KEYRING_ACTION, now=now)
        if not isinstance(verdict, Allowed):
            raise LostKeyringRefusedError(verdict)
        actor_id = principal.actor_id
        if confirmation != LOST_KEYRING_PHRASE:
            raise LostKeyringConfirmationError("type the confirmation phrase exactly")
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
