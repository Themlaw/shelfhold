"""The exits of a root that is not right: `root takeover|reinit|restored`, `store reinit`
(EF-EXP-064, EF-BIB-054 (8), EF-INT-023).

**States, per root.** A writable physical root whose sentinel is not the one the database expects
waits in one of four states, recorded in the `FreezeState` (one mark per root, no migration):

- `foreign_root`: the sentinel is another instance's, or cannot be trusted (unreadable, forged);
- `root_lost`: no sentinel: the disk was replaced by an empty one, or the file was removed;
- `root_behind`: the sentinel is older than the database by more than one step (the host put an
  old copy of the root back);
- `store_lost`: the same three, on `/store`.

The instance goes on; only that root is frozen (its writes, deletions and scans are refused by
`Fsops`, which already judged the sentinel). `RootWatcher` finds the states and records them; a
root whose sentinel is right again is released by the watcher alone.

**Exits.** All of them stamp or restamp a sentinel with `Fsops.takeover_sentinel` and none of them
deletes: not a user file, not a file of `.shelfhold/`.

- `takeover` keeps the root's database row and `root_id`, and adds the former identities to its
  predecessors; the files, the mirror and the tombstone journal stay where they are.
- `reinit` does the same stamp, and then says what the disk no longer holds: records of the root
  go to `missing` (the admin chooses to re-export or to stop following, `missing resolve`), the
  copies of the root stop counting as independent, the tombstone lines are owed to the new
  journal. Nothing is exported by it.
- `restored` (a root put back from an older copy) restamps the sentinel forward and puts the
  records whose file is not there any more back to `planned`: the next export writes them, through
  the pipeline and so through the parental barrier.
- `store reinit` is `reinit` for `/store`: the Units that lived only there go to `store_missing`.

**The sentinel is a claim.** The `root_id` of the stamped sentinel is the database's. When the
database has no row for the root (`/data` lost, the keyring imported), the `root_id` found in the
file is kept only if the keyring proves it (`RootEvidence`: a registry tag or a journal line that
opens under it); a file whose journal holds lines that open under nothing is refused
(`RootUnauthenticatedError`: a forged `root_id` would make every line inert and bring purged
content back), and a file with no evidence at all gets a new `root_id`. Predecessors and kids the
file declares are taken only from an authenticated file. The database half goes first, in one
transaction with the audit line; the sentinel follows, and a failure between the two is healed by
the watcher (the root is judged again).

**Concurrency.** Each exit takes the resource locks of the root's destinations
(`path:<destination>/<folder>`, plus `work:root-<root id>`) without waiting: an export or a cycle
that holds one makes the command answer `RootBusyError`, nothing changed.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.export.application.locks import LockBusyError, ResourceLocks
from shelfhold.export.application.root_bootstrap import RootPolicy, RootRefusedError
from shelfhold.export.application.roots import LoadedRoots, RootRow, load_roots, open_fsops
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.domain.path_map import PathMap, PathMapError, PathMode, clean_absolute, within
from shelfhold.export.domain.physical_roots import (
    SENTINEL_PREDECESSORS_FIELD,
    ExpectedRoot,
    RootState,
    SentinelDocument,
    judge_sentinel,
    takeover_document,
    waiting_state_of,
)
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import FsopsError, SentinelFormatError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.instance.generation import GenerationCounter
from shelfhold.infrastructure.schema import catalog, export, identity, platform
from shelfhold.library.public import (
    RootEvidence,
    distrust_root_copies,
)

__all__ = [
    "ExitReport",
    "RootBusyError",
    "RootExitError",
    "RootExits",
    "RootNotWaitingError",
    "RootUnauthenticatedError",
    "RootVerdict",
    "RootWatcher",
]

log = structlog.get_logger(__name__)

_PRESENT = ("written", "scanned", "resolved", "adopted")  # a record says the file is there
_MAX_PREDECESSORS = 64
_MAX_ID = 64


class RootExitError(Exception):
    """A root exit that was refused; nothing was changed."""

    code = "ops.not_waiting"


class RootNotWaitingError(RootExitError):
    code = "ops.not_waiting"


class RootUnauthenticatedError(RootExitError):
    code = "root.unauthenticated"


class RootBusyError(RootExitError):
    code = "root.busy"


class RootBlockedError(RootExitError):
    code = "root.blocked"


class RestoredBehindRefusedError(RootExitError):
    code = "instance.restored_behind"


class EvidenceSource(Protocol):
    def examine(self, root_path: str, candidates: Sequence[str]) -> dict[str, RootEvidence]: ...

    def tombstone_kids(self, root_path: str) -> dict[str, dict[str, str]]: ...


class TombstoneOwing(Protocol):
    def owe_lines(self, root_id: str) -> None: ...


@dataclass(frozen=True)
class RootVerdict:
    """One writable root against its sentinel, as the watcher saw it."""

    subject: str  # the `root_id`, or the path of a folder the database does not know
    path: str
    state: RootState
    waiting: str | None  # `foreign_root`... or None when nothing waits
    store: bool = False
    registered: bool = True
    found_instance: str | None = None


@dataclass(frozen=True)
class ExitReport:
    command: str
    root_id: str
    path: str
    previous: str  # the state the root was in
    sentinel: str  # `stamped` (new sentinel written), `restamped` (forward only)
    predecessors: tuple[str, ...] = ()
    missing: int = 0  # records now `missing` (reinit)
    planned: int = 0  # records now `planned` for the next export (restored)
    units_store_missing: int = 0
    distrusted_copies: int = 0
    ignored_claims: tuple[str, ...] = ()  # what an unauthenticated sentinel claimed and we ignored
    notes: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command, "root_id": self.root_id, "path": self.path,
            "previous": self.previous, "sentinel": self.sentinel,
            "predecessors": list(self.predecessors), "missing": self.missing,
            "planned": self.planned, "units_store_missing": self.units_store_missing,
            "distrusted_copies": self.distrusted_copies,
            "ignored_claims": list(self.ignored_claims), "notes": list(self.notes),
        }  # fmt: skip


# -- detection --------------------------------------------------------------------------------


def _look(fsops: Fsops, expected: ExpectedRoot) -> tuple[RootState | None, SentinelDocument | None]:
    """`(state, the sentinel found)`; state None when the folder is not there at all (a volume
    that is not mounted is the disk guard's and `target recheck`'s business, not a root state)."""
    if not os.path.isdir(expected.path):
        return None, None
    try:
        doc = fsops.sentinel_of(expected.path, strict=True)
    except (OSError, SentinelFormatError):
        return RootState.MISMATCH, None  # present but unreadable, a FIFO...: not trusted
    except FsopsError:
        return None, None
    return judge_sentinel(doc, expected).state, doc


class RootWatcher:
    """Finds the four states and records them in the `FreezeState` (EF-EXP-064)."""

    def __init__(
        self,
        engine: Engine,
        *,
        umask: int,
        store_path: str | None,
        freeze: FreezeState | None = None,
        on_root_ok: Callable[[], None] = lambda: None,
    ) -> None:
        self._engine = engine
        self._umask = umask
        self._store = _absolute_or_none(store_path)
        self._freeze = freeze
        self._on_root_ok = on_root_ok
        self._seen_ok: dict[str, bool] = {}
        self._told: dict[str, RootState] = {}

    def bind(self, freeze: FreezeState) -> None:
        """The `FreezeState` of the process that writes: it follows what is recorded."""
        self._freeze = freeze

    def assess(self) -> list[RootVerdict]:
        """Read only."""
        with self._engine.connect() as connection:
            head = connection.execute(sa.select(platform.instance.c.instance_id)).first()
            paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
            if head is None or not paths:
                return []
            loaded = load_roots(connection, PathMap.same_path(paths))
        fsops = open_fsops(
            loaded, umask=self._umask, generation_source=GenerationCounter(self._engine).read
        )
        found: list[RootVerdict] = []
        for expected in loaded.expected:
            state, doc = _look(fsops, expected)
            if state is None:
                continue
            store = clean_absolute(expected.path) == self._store
            found.append(
                RootVerdict(
                    expected.root_id,
                    expected.path,
                    state,
                    waiting_state_of(state, store=store),
                    store,
                    True,
                    doc.instance_id if doc else None,
                )
            )
        return found

    def refresh(self) -> list[RootVerdict]:
        """Judge the roots, record the states that wait and release the roots that are right
        again. Idempotent; a state already recorded is not written twice."""
        verdicts = self.assess()
        back = False
        for verdict in verdicts:  # a root that is right again (or first seen so) is replayed
            ok = verdict.state is RootState.OK
            back = back or (ok and not self._seen_ok.get(verdict.subject, False))
            self._seen_ok[verdict.subject] = ok
        if back:
            self._on_root_ok()
        with self._engine.connect() as connection:  # a look: nothing is locked for it
            owed = self._marks_owed(verdicts, FreezeState.stored_roots(connection))
        if owed:  # the write lock is taken once, for the marks only (`busy_timeout` waits for it)
            with self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE").begin() as conn:
                for verdict in self._marks_owed(verdicts, FreezeState.stored_roots(conn)):
                    if verdict.waiting is None:
                        FreezeState().lift_root(conn, verdict.subject)
                    else:
                        FreezeState().persist_root(
                            conn, verdict.subject, verdict.waiting,
                            reason=_REASONS[verdict.waiting], path=verdict.path,
                        )  # fmt: skip
        if self._freeze is not None:
            with self._engine.connect() as connection:
                self._freeze.sync(connection)
        return verdicts

    @staticmethod
    def _marks_owed(verdicts: list[RootVerdict], stored: dict[str, str]) -> list[RootVerdict]:
        """The verdicts whose mark is to be written or lifted: what is recorded already, and what
        waits for its exit command, is left alone (no write, no generation, no sentinel)."""
        owed: list[RootVerdict] = []
        for verdict in verdicts:
            current = stored.get(verdict.subject)
            if verdict.state is RootState.RESTORED_BEHIND:
                continue  # the instance's own state (EF-INT-026): not a root mark
            if verdict.waiting is None:
                if current == "root_lost":  # a vanished sentinel that came back
                    owed.append(verdict)
            elif current != verdict.waiting:
                owed.append(verdict)
        return owed

    def note_blocked(self, root_path: str, state: RootState) -> None:
        """`Fsops` refused a write on this root: look again, once per change of state."""
        if self._told.get(root_path) is state:
            return
        self._told[root_path] = state
        self.refresh()


_REASONS = {
    "foreign_root": "the sentinel of this root is another instance's, or cannot be trusted",
    "root_lost": "this root has no sentinel: the disk was replaced, or the file was removed",
    "root_behind": "this root is older than the database: an old copy was put back",
    "store_lost": "the sentinel of /store is missing, foreign or behind the database",
}


# -- the exits --------------------------------------------------------------------------------


@dataclass
class _Target:
    row: RootRow | None
    path: str
    expected: ExpectedRoot | None
    state: RootState
    found: SentinelDocument | None
    store: bool

    @property
    def subject(self) -> str:
        return self.row.root_id if self.row else self.path


@dataclass
class _Plan:
    """What the stamped sentinel will say, and what was refused to the file."""

    root_id: str
    predecessors: tuple[str, ...]
    kids: list[dict[str, object]]
    trusted: bool
    ignored: tuple[str, ...]


class RootExits:
    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock,
        umask: int,
        store_path: str | None,
        locks: ResourceLocks,
        watcher: RootWatcher,
        evidence: Callable[[Fsops], EvidenceSource | None] = lambda _fsops: None,
        tombstones: Callable[[], TombstoneOwing | None] = lambda: None,
        roots_changed: Callable[[], None] = lambda: None,
        policy: RootPolicy | None = None,
        fresh_auth_s: int = 900,
        freeze: FreezeState | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._umask = umask
        self._store = _absolute_or_none(store_path)
        self._locks = locks
        self._watcher = watcher
        self._evidence = evidence
        self._tombstones = tombstones
        self._roots_changed = roots_changed
        self._policy = policy
        self._fresh_auth_s = fresh_auth_s
        self._freeze = freeze

    # -- the four commands ----------------------------------------------------------------

    def takeover(self, subject: str, *, principal: Principal) -> ExitReport:
        """Keep the root as the database knows it, stamp it as ours."""
        self._authorize(principal, "root.takeover")
        target, fsops = self._target(subject, store=False, may_be_new=True)
        self._require_state(target, {"foreign_root", "root_lost", "root_behind"})
        with self._locked(target):
            plan = self._plan(target, fsops)
            self._commit_identity(
                target, plan, principal, "root.takeover", then=lambda conn, rid: None
            )
            self._stamp(fsops, target, plan)
        return self._report("root takeover", target, plan, "stamped")

    def reinit(self, subject: str, *, principal: Principal) -> ExitReport:
        """The disk is not what the database remembers: stamp it, and say what is gone."""
        self._authorize(principal, "root.reinit")
        target, fsops = self._target(subject, store=False, may_be_new=False)
        self._require_state(target, {"foreign_root", "root_lost", "root_behind"})
        counts = {"missing": 0, "copies": 0}

        def database(conn: Connection, root_id: str) -> None:
            counts["missing"] = self._mark_records(conn, target, "missing", only_absent=False)
            counts["copies"] = distrust_root_copies(conn, root_id)

        with self._locked(target):
            plan = self._plan(target, fsops)
            self._commit_identity(target, plan, principal, "root.reinit", then=database)
            self._stamp(fsops, target, plan)
            self._owe_lines(plan.root_id)
        return self._report(
            "root reinit", target, plan, "stamped", missing=counts["missing"],
            distrusted=counts["copies"],
        )  # fmt: skip

    def restored(self, subject: str, *, principal: Principal | None = None) -> ExitReport:
        """A root put back from an older copy: the sentinel catches up, the files that the copy
        lacks are planned again for the next export."""
        del principal  # not a reserved exit: it deletes nothing and writes no file by itself
        target, fsops = self._target(subject, store=False, may_be_new=False)
        self._require_state(target, {"root_behind"})
        if target.row is None:
            raise RootNotWaitingError("no such root")
        counts = {"planned": 0, "copies": 0}
        epoch = self._epoch()
        with self._locked(target):
            with self._engine.begin() as connection:
                counts["planned"] = self._mark_records(
                    connection, target, "planned", only_absent=True, fsops=fsops
                )
                counts["copies"] = distrust_root_copies(connection, target.row.root_id)
                self._lift(connection, target.row.root_id)
                self._audit(connection, None, "root.restored", target.row.root_id)
            try:
                fsops.restamp_after_restore(
                    target.path, generation=target.row.generation, restore_epoch=epoch
                )
            except FsopsError:
                self._watcher.refresh()  # the root is judged again, the mark comes back
                raise
            self._owe_lines(target.row.root_id)
            self._follow(target.row.root_id)
        plan = _Plan(target.row.root_id, target.row.predecessors, [], True, ())
        return self._report(
            "root restored",
            target,
            plan,
            "restamped",
            planned=counts["planned"],
            distrusted=counts["copies"],
        )

    def store_reinit(self, *, principal: Principal) -> ExitReport:
        """`/store` is not what the database remembers: stamp it; the Units whose kept copy was
        there are `store_missing`. Nothing is deleted, nothing is downloaded again by it."""
        self._authorize(principal, "store.reinit")
        if self._store is None:
            raise RootNotWaitingError("this instance has no /store")
        target, fsops = self._target(self._store, store=True, may_be_new=True)
        self._require_state(target, {"store_lost"})
        counts = {"units": 0, "copies": 0}

        def database(conn: Connection, root_id: str) -> None:
            counts["units"] = self._store_units_missing(conn, root_id)
            counts["copies"] = distrust_root_copies(conn, root_id)

        with self._locked(target):
            plan = self._plan(target, fsops)
            self._commit_identity(target, plan, principal, "store.reinit", then=database)
            self._stamp(fsops, target, plan)
            self._owe_lines(plan.root_id)
        return self._report(
            "store reinit", target, plan, "stamped", units=counts["units"],
            distrusted=counts["copies"],
        )  # fmt: skip

    # -- reading the situation --------------------------------------------------------------

    def _authorize(self, principal: Principal, action: str) -> None:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS[action], now=self._clock.now(),
            fresh_auth_s=self._fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the route or the socket decided first
            raise PermissionError(type(verdict).__name__)

    def _epoch(self) -> int:
        with self._engine.connect() as connection:
            return int(
                connection.execute(sa.select(platform.instance.c.restore_epoch)).scalar() or 0
            )

    def _target(self, subject: str, *, store: bool, may_be_new: bool) -> tuple[_Target, Fsops]:
        with self._engine.connect() as connection:
            head = connection.execute(
                sa.select(platform.instance.c.instance_id, platform.instance.c.restore_epoch)
            ).first()
            if head is None:
                raise RootNotWaitingError("no instance is registered yet")
            if FreezeState.read(connection).frozen:
                raise RestoredBehindRefusedError(
                    "restored_behind: run `shelfhold reconcile --adopt-after-restore` first"
                )
            paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
            loaded = load_roots(connection, PathMap.same_path(paths) if paths else PathMap(()))
        row = next(
            (r for r in loaded.roots if r.root_id == subject or r.path == _clean(subject)), None
        )
        expected_roots = list(loaded.expected)
        if row is None:
            path = _clean(subject)
            if not may_be_new or not posixpath.isabs(subject) or not os.path.isdir(path):
                raise RootNotWaitingError("no such root")
            self._check_new(path, loaded)
            # the root the folder would become: judged like a registered one (a sentinel there
            # is always another's), and readable through `Fsops` like one
            expected_roots.append(
                ExpectedRoot(new_ulid(), path, str(head.instance_id), 0, int(head.restore_epoch))
            )
            path_map = PathMap.same_path([*paths, path])
        else:
            path, path_map = row.path, loaded.path_map
        if (clean_absolute(path) == self._store) != store:
            raise RootNotWaitingError(
                "this is /store: use `shelfhold store reinit`"
                if not store
                else "this is not /store"
            )
        if row is not None and row.mode is not PathMode.RW:
            raise RootNotWaitingError("a read-only root has no sentinel")
        expected = next((e for e in expected_roots if e.path == path), None)
        if expected is None:
            raise RootNotWaitingError("no such root")
        fsops = Fsops.open(
            path_map,
            expected_roots,
            umask=self._umask,
            generation_source=GenerationCounter(self._engine).read,
        )
        state, found = _look(fsops, expected)
        if state is None:
            raise RootBlockedError(f"{path} is not mounted")
        if state is RootState.RESTORED_BEHIND:
            raise RestoredBehindRefusedError(
                "restored_behind: the database is older than this root: "
                "run `shelfhold reconcile --adopt-after-restore`"
            )
        return _Target(row, path, expected, state, found, store), fsops

    def _check_new(self, path: str, loaded: LoadedRoots) -> None:
        known = [r.path for r in loaded.roots]
        if any(within(path, other) is not None or within(other, path) is not None
               for other in known):  # fmt: skip
            raise RootRefusedError(f"{path} overlaps a registered root")
        if self._policy is not None:
            self._policy.check(path, PathMode.RW, known)

    @staticmethod
    def _require_state(target: _Target, wanted: set[str]) -> None:
        state = waiting_state_of(target.state, store=target.store)
        if state not in wanted:
            raise RootNotWaitingError(
                "this root is not waiting in a state this command leaves "
                f"(it is {state or 'right'}; the command leaves {', '.join(sorted(wanted))})"
            )

    # -- what the stamped sentinel will say --------------------------------------------------

    def _plan(self, target: _Target, fsops: Fsops) -> _Plan:
        found = target.found
        source = self._evidence(fsops)
        candidates: list[str] = []
        if target.row:
            candidates.append(target.row.root_id)
        if found is not None and found.root_id not in candidates:
            candidates.append(found.root_id)
        evidence = source.examine(target.path, candidates) if source and candidates else {}
        authentic = {rid for rid, e in evidence.items() if e.authentic}
        ignored: list[str] = []
        if target.row:
            root_id = target.row.root_id  # the database's: a file never chooses it
        elif found is not None and found.root_id in authentic:
            root_id = found.root_id
            self._refuse_borrowed_identity(target.path, root_id, evidence[root_id])
        elif found is not None and (
            source is None or evidence.get(found.root_id, _NONE).contradicted
        ):
            raise RootUnauthenticatedError(
                "the sentinel claims a root_id that the keyring cannot confirm and the tombstone "
                "journal holds lines that do not open under it: import the keyring "
                "(`shelfhold secrets import`) and try again"
            )
        else:
            root_id = new_ulid()
            if found is not None:
                ignored.append("root_id")
        trusted = found is not None and bool(authentic)
        chain: list[str] = list(target.row.predecessors) if target.row else []
        if found is not None and trusted:
            chain.append(found.instance_id)
            if found.root_id != root_id and found.root_id in authentic:
                chain.append(found.root_id)  # its journal lines open under it: keep them readable
            chain.extend(_declared(found))
        elif found is not None:
            ignored.append("predecessors")
        kids: list[dict[str, object]] = []
        verified = evidence.get(root_id)
        if source is not None and verified is not None and verified.kids and found is not None:
            tags = source.tombstone_kids(target.path)
            kids = [{"kid": k, "tags": dict(tags[k])} for k in sorted(verified.kids) if k in tags]
        bounded = tuple(dict.fromkeys(c for c in chain if _plausible(c)))[:_MAX_PREDECESSORS]
        return _Plan(root_id, bounded, kids, trusted, tuple(ignored))

    def _refuse_borrowed_identity(self, path: str, root_id: str, proof: RootEvidence) -> None:
        """A `root_id` is kept from a file only when it is really this folder's: not the one of
        another registered root (a sentinel copied from `/store`), and not one under which the
        journal of this folder cannot be read (its lines would be inert and a purge forgotten)."""
        with self._engine.connect() as connection:
            elsewhere = connection.execute(
                sa.select(platform.physical_root.c.path_entry).where(
                    platform.physical_root.c.root_id == root_id,
                    platform.physical_root.c.path_entry != path,
                )
            ).first()
        if elsewhere is not None:
            raise RootUnauthenticatedError(
                f"the sentinel carries the root_id of {elsewhere.path_entry}, another root of "
                "this instance: a sentinel copied from one root to another is never kept"
            )
        if proof.lines > 0 and proof.opened == 0:
            raise RootUnauthenticatedError(
                "the tombstone journal of this folder holds lines that do not open under the "
                "root_id of its sentinel: keeping it would forget purges. Import the keyring "
                "(`shelfhold secrets import`) or put the journal aside, then try again"
            )

    def _document(
        self, target: _Target, plan: _Plan, instance_id: str, epoch: int
    ) -> SentinelDocument:
        generation = target.row.generation if target.row else 1
        return takeover_document(
            target.found if plan.trusted else None, root_id=plan.root_id,
            instance_id=instance_id, generation=generation, restore_epoch=epoch,
            predecessors=plan.predecessors, kids=plan.kids,
        )  # fmt: skip

    # -- the database half, then the sentinel -------------------------------------------------

    def _commit_identity(
        self,
        target: _Target,
        plan: _Plan,
        principal: Principal,
        action: str,
        *,
        then: Callable[[Connection, str], None],
    ) -> None:
        with self._engine.begin() as connection:
            if target.row is None:
                connection.execute(
                    sa.insert(platform.physical_root).values(
                        root_id=plan.root_id,
                        path_entry=target.path,
                        mode="rw",
                        generation=1,
                        predecessor_instance_ids=list(plan.predecessors),
                    )
                )
            else:
                connection.execute(
                    sa.update(platform.physical_root)
                    .where(platform.physical_root.c.root_id == plan.root_id)
                    .values(predecessor_instance_ids=list(plan.predecessors))
                )
            then(connection, plan.root_id)
            for subject in {target.subject, plan.root_id}:
                FreezeState().lift_root(connection, subject)
            self._audit(connection, principal, action, plan.root_id, plan.ignored)

    def _stamp(self, fsops: Fsops, target: _Target, plan: _Plan) -> None:
        with self._engine.connect() as connection:
            instance_id = str(
                connection.execute(sa.select(platform.instance.c.instance_id)).scalar_one()
            )
        epoch = self._epoch()
        try:
            fsops.takeover_sentinel(
                target.path,
                lambda _found, _damaged: self._document(target, plan, instance_id, epoch),
            )
        except (FsopsError, OSError, SentinelFormatError):
            self._watcher.refresh()  # the database half is done: the root is judged again
            raise
        self._follow(plan.root_id)
        if target.row is None:
            self._follow(target.path)

    def _follow(self, subject: str) -> None:
        self._roots_changed()  # every exit ends here: the tombstone index is replayed again
        if self._freeze is not None:
            self._freeze.follow_root(subject, None)

    def _lift(self, connection: Connection, root_id: str) -> None:
        FreezeState().lift_root(connection, root_id)

    def _audit(
        self, connection: Connection, principal: Principal | None, action: str, root_id: str,
        ignored: Sequence[str] = (),
    ) -> None:  # fmt: skip
        target = f"root:{root_id}" + (f";ignored:{','.join(ignored)}" if ignored else "")
        connection.execute(
            sa.insert(identity.audit).values(
                id=new_ulid(),
                actor_id=principal.actor_id if principal else "system",
                action=action,
                target=target,
                session_id=principal.session_id if principal else None,
                auth_time=principal.auth_time if principal else None,
            )
        )

    def _owe_lines(self, root_id: str) -> None:
        service = self._tombstones()
        if service is not None:
            service.owe_lines(root_id)  # the journal of this root is rewritten from the table

    # -- records and units ------------------------------------------------------------------

    def _destinations(self, connection: Connection, root_path: str) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for destination_id in connection.execute(
            sa.select(export.shelf_destination.c.id)
        ).scalars():
            local = load_context(connection, destination_id).root_local
            if local is not None and within(clean_absolute(local), root_path) is not None:
                found.append((destination_id, clean_absolute(local)))
        return sorted(found)

    def _mark_records(
        self, connection: Connection, target: _Target, state: str, *, only_absent: bool,
        fsops: Fsops | None = None,
    ) -> int:  # fmt: skip
        record = export.export_record
        changed = 0
        for destination_id, local in self._destinations(connection, target.path):
            rows = connection.execute(
                sa.select(record.c.export_item_id, record.c.rel_path).where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.state.in_(_PRESENT),
                    record.c.rel_path.is_not(None),
                )
            ).all()
            for row in rows:
                if (
                    only_absent
                    and fsops is not None
                    and fsops.observe(f"{local}/{row.rel_path}") is not None
                ):
                    continue
                changed += connection.execute(
                    sa.update(record)
                    .where(
                        record.c.shelf_destination_id == destination_id,
                        record.c.export_item_id == row.export_item_id,
                    )
                    .values(state=state, error_code=None, updated_at=self._clock.now())
                ).rowcount
        return changed

    @staticmethod
    def _store_units_missing(connection: Connection, root_id: str) -> int:
        del root_id  # `/store` holds the kept copies of every Unit that is `stored`
        return connection.execute(
            sa.update(catalog.unit)
            .where(catalog.unit.c.state == "stored")
            .values(state="store_missing")
        ).rowcount

    # -- the locks --------------------------------------------------------------------------

    @contextmanager
    def _locked(self, target: _Target) -> Iterator[None]:
        resources = [f"work:root-{target.row.root_id}"] if target.row else []
        if target.row:
            with self._engine.connect() as connection:
                for destination_id, local in self._destinations(connection, target.path):
                    folders = {
                        rel.split("/", 1)[0]
                        for rel in connection.execute(
                            sa.select(export.export_record.c.rel_path).where(
                                export.export_record.c.shelf_destination_id == destination_id,
                                export.export_record.c.rel_path.is_not(None),
                            )
                        ).scalars()
                        if rel
                    }
                    resources += [f"path:{destination_id}/{folder}" for folder in sorted(folders)]
                    del local
                # what an export really takes: the `work` of each series it writes (a series about
                # to be created has a Work too) and the `path` of its folder
                works = connection.execute(
                    sa.select(catalog.edition.c.work_id)
                    .select_from(
                        export.export_record.join(
                            export.export_item,
                            export.export_item.c.id == export.export_record.c.export_item_id,
                        ).join(
                            catalog.edition, catalog.edition.c.id == export.export_item.c.edition_id
                        )
                    )
                    .where(
                        export.export_record.c.shelf_destination_id.in_(
                            [d for d, _l in self._destinations(connection, target.path)]
                        )
                    )
                    .distinct()
                ).scalars()
                resources += [f"work:{w}" for w in sorted(works)]
        job_id = new_ulid()
        if not resources:
            yield
            return
        try:
            with self._locks.hold_resources(job_id, resources, timeout=0.0):
                yield
        except LockBusyError as busy:
            raise RootBusyError(
                f"{busy.resource} is held by an export or a cycle: try again when it is over"
            ) from None

    def _report(
        self, command: str, target: _Target, plan: _Plan, sentinel: str, *, missing: int = 0,
        planned: int = 0, units: int = 0, distrusted: int = 0,
    ) -> ExitReport:  # fmt: skip
        return ExitReport(
            command, plan.root_id, target.path,
            waiting_state_of(target.state, store=target.store) or "", sentinel,
            plan.predecessors, missing, planned, units, distrusted, plan.ignored,
        )  # fmt: skip


_NONE = RootEvidence("", frozenset(), 0, 0)


def _absolute_or_none(path: str | None) -> str | None:
    """A relative `/store` setting is a configuration problem reported elsewhere (L41): it never
    stops the start, it only means no folder is taken for the store here."""
    try:
        return clean_absolute(path) if path else None
    except PathMapError:
        return None


def _clean(path: str) -> str:
    try:
        return clean_absolute(path)
    except ValueError:
        return path


def _plausible(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_ID and value.isascii()


def _declared(found: SentinelDocument) -> list[str]:
    """The predecessors an authenticated sentinel declares (a list of short strings)."""
    value = found.extra.get(SENTINEL_PREDECESSORS_FIELD)
    if not isinstance(value, list):
        return []
    return [v for v in value[:_MAX_PREDECESSORS] if _plausible(v)]
