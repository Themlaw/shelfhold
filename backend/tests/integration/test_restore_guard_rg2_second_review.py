"""Second adverse review of the whole-instance restore guard (RG2, EF-INT-026, I39, ADR-0044).

#387 closed the shared lease of `Composition._hold`: a job that joins a held session looks at
the whole instance, and `WriteSessions.check` looks once per heartbeat while a session is open.
This review asked what else writes on the roots, and what dates the verdict of the heartbeat.
Two holes, both outside the inventory of the fix (`docs/decisions/M5.md`, "Suivi RG2"):

- **The removal of a destination writes with no look at all.** `target remove --delete-files`
  (the job `destination.remove` it queues, and `detach --full` behind it, whose `_erase_hidden`
  erases the host area of the roots the same way) goes through `DetachOps`, which builds
  `RemoveDestination` over the `Fsops` of `Composition.read_context()`. No session is opened, so
  the whole-instance look of `WriteSessions.open` never runs; no lease verifier is ever bound to
  that `Fsops` (only `write_context` and `session_opener` enforce theirs), so no lease is asked of
  it either. A root that comes back ahead of the database is invisible to the removal: the files
  Shelfhold wrote leave the disk for good, and the manifest is forgotten on the database the
  guard already calls older than the disk.
- **The verdict of the heartbeat is dated on a clock that can step back.** The cache holds while
  `now - _looked_at < HEARTBEAT_INTERVAL`, on the wall clock the leases are dated with. A clock
  correction backwards — the very anomaly `Lease.expired` tolerates for `DEFAULT_SKEW` (five
  minutes) before it calls the lease lost — keeps a verdict taken before the step for as long as
  it takes the clock to climb back above it: minutes of writes on the matching roots, past the
  "at most one heartbeat of writes" the fix documents.

Checked, no hole: once the guard has counted a restore (the epoch moved), every root reads
`restored_behind` at its own verdict and the removal is refused file by file, freeze fork or
not; a new `WriteSessions` is built for every held lease, so no verdict survives a closure; the
stamp lock is the process's, and the writers of another process take the instance lock (the ops
socket runs in the service itself).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.composition import open_fsops
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.remove_destination import (
    RemovalMode,
    RemoveDestination,
    removal_phrase,
)
from shelfhold.export.application.root_ref import RootRef
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.instance import HEARTBEAT_INTERVAL, WriteSessions
from shelfhold.infrastructure.instance.generation import GenerationCounter
from shelfhold.infrastructure.schema import export
from tests.restore_env import RestoreEnv, restore_env

pytestmark = pytest.mark.req("EF-INT-026", "EF-EXP-029")

SENTINEL = ".shelfhold-mount-id"

HOST = Principal(  # the host console, as the local CLI socket presents it to the ops surface
    "01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI
)


def _put_ahead(root: Path) -> None:
    """The root reads as ahead of the database: what a restore of `/data` leaves (ADR-0044)."""
    path = root / SENTINEL
    document = json.loads(path.read_text("utf-8"))
    document["generation"] = int(document["generation"]) + 5
    path.write_text(json.dumps(document), "utf-8")


class _Monotonic:
    """A monotonic clock moved by hand: the wall clock of the test can step back, this cannot."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _sessions_of(
    env: RestoreEnv, behind: list[bool], monotonic: _Monotonic | None = None
) -> WriteSessions:
    """What `Composition.sessions_for` wires: the guard is asked when a root is ahead."""
    sessions = WriteSessions(
        env.fsops,
        instance_id=str(env.sentinel()["instance_id"]),
        clock=env.ex.clock,
        generations=GenerationCounter(env.engine),
        on_behind=lambda: behind.append(env.guard().enter_restored_behind().behind),
        monotonic=monotonic or _Monotonic(),
    )
    sessions.enforce()
    return sessions


def _remover_of(env: RestoreEnv) -> RemoveDestination:
    """The wiring of `target remove` (ops/detach.py: `DetachOps._context` and `_remover` over
    `Composition.read_context()`): the `Fsops` no verifier is ever bound to, the journal, and
    the removal over them. No session is opened anywhere on this path."""
    fsops = open_fsops(
        env.loaded, umask=0o022, generation_source=GenerationCounter(env.engine).read
    )
    fsops.refuse_writes()  # what `Composition.read_context()` does: the view writes nothing
    journal = IntentJournal(env.engine, fsops, clock=env.ex.clock)
    rows = {r.root_id: r for r in env.loaded.roots}

    def roots(local: str) -> RootRef | None:
        root_id = env.loaded.root_id_for(local)
        return None if root_id is None else RootRef(root_id, rows[root_id].path)

    return RemoveDestination(
        env.engine, fsops, journal, clock=env.ex.clock, roots=roots,
        locks=ResourceLocks(env.engine, clock=env.ex.clock), request_scan=None,
    )  # fmt: skip


def _count(env: RestoreEnv, table: sa.Table) -> int:
    with env.engine.connect() as connection:
        return int(connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


# -- the control: the door the branch already closes ------------------------------------------


def test_control_a_session_writer_is_refused_once_the_store_is_ahead() -> None:
    """The scenario of every proof below: the store comes back ahead of the database, the
    library still matches its row. A writer that opens a session is refused before it writes,
    and the guard freezes the whole instance (ADR-0044)."""
    with restore_env() as env:
        _put_ahead(env.ex.lib.store)
        assert env.guard().assess().behind  # the instance, not the library alone
        behind: list[bool] = []
        kept = env.ex.lib.store / "kept.bin"
        with pytest.raises(RootBlockedError), _sessions_of(env, behind).open(str(env.library)):
            env.fsops.write_atomic(str(kept), b"kept", parents=True)
        assert behind == [True]
        assert not kept.exists()


# -- the removal: a writer with no session, no lease and no look ------------------------------


def test_the_removal_of_a_destination_deletes_while_the_store_is_ahead() -> None:
    """The same scenario through `target remove --delete-files`: the files live on the library,
    which still matches, while the store is ahead of the database. The removal builds its own
    door (the fsops of `Composition.read_context()`): the look of `WriteSessions.open` never
    runs, no lease is required of it, and the guard is never asked. What Shelfhold wrote leaves
    the disk for good and the manifest is forgotten on the database the guard already calls
    older than the disk."""
    with restore_env() as env:
        dest = env.ex.destination_id
        _put_ahead(env.ex.lib.store)
        assert env.guard().assess().behind
        before = env.files()
        rows = _count(env, export.export_record)
        # the view refuses each deletion (`ReadOnlyFsopsError`): the removal leaves every file
        report = _remover_of(env).apply(
            dest,
            RemovalMode.DELETE_FILES,
            principal=HOST,
            typed=removal_phrase(dest, RemovalMode.DELETE_FILES),
        )
        assert report.deleted == 0, report.left  # read only as a whole: nothing may leave
        assert env.files() == before  # read only as a whole: nothing leaves the disk
        assert _count(env, export.export_record) == rows  # and the manifest is kept


# -- the heartbeat: a verdict dated on a clock that can step back -----------------------------


def test_a_heartbeat_elapsed_after_a_clock_step_back_looks_again() -> None:
    """A held session, the library comes back ahead, and the clock steps back four minutes —
    inside the `DEFAULT_SKEW` the lease machinery itself tolerates (`Lease.expired` calls a
    lease dated ahead run out only past five minutes). A heartbeat then elapses; the verdict of
    the look taken before the step is still served (`now - _looked_at` is negative, so it counts
    as fresh), and the session keeps writing on the store: past the "at most one heartbeat of
    writes" the fix documents, for as long as it takes the clock to climb back above the stamp
    of the last look."""
    with restore_env() as env:
        behind: list[bool] = []
        mono = _Monotonic()
        kept = env.ex.lib.store / "kept.bin"
        with _sessions_of(env, behind, mono).open(str(env.ex.lib.store)) as session:
            session.write_atomic(str(kept), b"first", parents=True)
            _put_ahead(env.library)  # the library comes back ahead of the database
            env.ex.clock.advance(seconds=-4 * 60)  # the correction, inside the lease skew
            env.ex.clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)
            mono.now += HEARTBEAT_INTERVAL.total_seconds() + 1  # the real time that went by
            with pytest.raises(RootBlockedError):  # a heartbeat has gone by: look again
                session.write_atomic(str(kept), b"second", parents=True)
        assert behind == [True]
        assert kept.read_bytes() == b"first"  # the refused write never landed
