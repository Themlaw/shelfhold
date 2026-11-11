"""Upkeep of SQLite (ENF-OPS-016): the database and each Huey queue, one file at a time.

- `PRAGMA optimize`, daily, at any hour: it reads statistics and writes only those;
- checkpoint `TRUNCATE` of the WAL, every night, in the window: the log goes back to zero bytes.
  A reader that holds a snapshot (an SSE stream that keeps a transaction open) makes the
  truncation wait; after `CHECKPOINT_WAIT_S` it is reported as partial and left to the next night,
  never raised: the frames already copied are not lost;
- `VACUUM`, once every 28 days, in the window only.

A VACUUM rewrites the whole file and takes the exclusive lock, so it never starts under any of
these (each answers with an `outcome`, and the job tries again the next night, the monthly
record being written only when every file is done):

- the instance is frozen (`restored_behind`, `FreezeState`): nothing is written until the adoption;
- the window is closed (also checked by the scheduler, which only queues it inside);
- another job is running: it may be a long write, and the lock it needs is the one VACUUM takes;
- the `MaintenanceGate` is held: the integrated backup (L23) holds it for its whole run, so that
  the file it copies is not rewritten under it, and a VACUUM in progress makes it wait;
- the volume has not twice the size of the largest file free (a VACUUM works on a copy).
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.schema import platform
from shelfhold.infrastructure.wal import settle_pending, truncate_wal
from shelfhold.jobs.huey_app import JobContext, JobPools
from shelfhold.jobs.window import MaintenanceWindow, WindowRun

__all__ = [
    "CHECKPOINT_KIND",
    "OPTIMIZE_KIND",
    "VACUUM_KIND",
    "CheckpointReport",
    "MaintenanceBusyError",
    "MaintenanceGate",
    "SqliteMaintenance",
    "VacuumReport",
    "compact_database",
    "register_sqlite_maintenance",
]

log = structlog.get_logger(__name__)

OPTIMIZE_KIND: Final = "sqlite_optimize"
CHECKPOINT_KIND: Final = "wal_checkpoint"  # the names of WINDOW_JOB_ORDER (ENF-OPS-014)
VACUUM_KIND: Final = "vacuum_monthly"
OPTIMIZE_CRON: Final = "10 2 * * *"
CHECKPOINT_CRON: Final = "5 4 * * *"  # clamped to the start of the window
VACUUM_CRON: Final = "30 4 * * *"  # a try every night; one success per VACUUM_EVERY (monthly)
VACUUM_EVERY: Final = timedelta(days=28)
VACUUM_RECORD: Final = "maintenance.vacuum.last"
CHECKPOINT_WAIT_S: Final = 0.5
VACUUM_WAIT_S: Final = 5.0


class MaintenanceBusyError(RuntimeError):
    """The gate is held in a way that excludes this holder (a VACUUM is running)."""


class MaintenanceGate:
    """Who is using the database in a way a VACUUM must not interrupt (L23 takes it for a backup).

    Holders are shared among themselves; the VACUUM is exclusive: it starts only if nobody holds
    the gate, and while it runs `hold` waits (up to `wait_s`) then raises `MaintenanceBusyError`.

    With a `lock_path` the gate also reaches other processes: a holder takes a shared `flock` on
    that file, the VACUUM an exclusive one. That is how `shelfhold backup run`, a separate process
    that cannot see the server's gate, still excludes (and is excluded by) a VACUUM. The file is
    never deleted and holds nothing.
    """

    def __init__(self, lock_path: Path | None = None) -> None:
        self._cond = threading.Condition()
        self._holders: list[str] = []
        self._exclusive = False
        self._lock_path = lock_path

    def holders(self) -> list[str]:
        with self._cond:
            return list(self._holders)

    def _flock(self, mode: int, wait_s: float) -> int | None:
        """A descriptor holding the file lock in `mode`, or None if it could not be had within
        `wait_s` (0: one try). No `lock_path`: a placeholder that holds nothing (-1)."""
        if self._lock_path is None:
            return -1
        descriptor = os.open(
            self._lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    return None
                time.sleep(0.2)

    @contextmanager
    def hold(self, name: str, *, wait_s: float = 0.0) -> Iterator[None]:
        with self._cond:
            if not self._cond.wait_for(lambda: not self._exclusive, timeout=wait_s or None) and (
                self._exclusive
            ):
                raise MaintenanceBusyError("a VACUUM is running")
            self._holders.append(name)
        descriptor: int | None = None
        try:
            descriptor = self._flock(fcntl.LOCK_SH, wait_s)
            if descriptor is None:
                raise MaintenanceBusyError("a VACUUM is running in another process")
            yield
        finally:
            if descriptor is not None and descriptor >= 0:
                os.close(descriptor)  # closing the descriptor drops the lock
            with self._cond:
                self._holders.remove(name)
                self._cond.notify_all()

    @contextmanager
    def exclusive(self) -> Iterator[bool]:
        """True if the gate was free and is now ours; False, and nothing held, otherwise."""
        descriptor: int | None = None
        with self._cond:
            free = not self._holders and not self._exclusive
            if free:
                descriptor = self._flock(fcntl.LOCK_EX, 0.0)
                free = descriptor is not None
            if free:
                self._exclusive = True
        try:
            yield free
        finally:
            if free:
                with self._cond:
                    self._exclusive = False
                    self._cond.notify_all()
                if descriptor is not None and descriptor >= 0:
                    os.close(descriptor)


def compact_database(database: Path, gate: MaintenanceGate) -> None:
    """`VACUUM` of one file now, under the exclusive gate, then the log is cut: the free pages of
    rows deleted before `secure_delete` was on (a title of an earlier purge) are rewritten away.
    Cost: the whole file is rewritten (about its size in reads and writes, and in free room), and
    writers wait for it. A gate held (a backup, the nightly VACUUM) is `MaintenanceBusyError`."""
    with gate.exclusive() as ours:
        if not ours:
            raise MaintenanceBusyError("the database is in use by a backup or a VACUUM")
        connection = sqlite3.connect(database, timeout=VACUUM_WAIT_S, isolation_level=None)
        try:
            connection.execute("VACUUM")
        finally:
            connection.close()
        truncate_wal(database, mark_on_failure=True)  # a reader keeps the log: `WalBusyError`
    log.info("sqlite.compacted", database=database.name)


class SpaceCheck(Protocol):
    def has_room_for_vacuum(self, *, database_bytes: int, volume: str) -> bool: ...


@dataclass(frozen=True)
class CheckpointReport:
    databases: int = 0
    partial: list[str] = field(default_factory=list)  # the files a reader kept from truncating
    frozen: bool = False


@dataclass(frozen=True)
class VacuumReport:
    outcome: str  # done | preempted | frozen | outside_window | not_due | busy | no_room
    databases: int = 0


class SqliteMaintenance:
    def __init__(
        self,
        engine: Engine,
        *,
        queue_files: dict[str, Path],
        clock: Clock,
        window: MaintenanceWindow,
        space: SpaceCheck,
        gate: MaintenanceGate,
        freeze: FreezeState | None = None,
    ) -> None:
        database = engine.url.database
        if database is None:
            raise ValueError("an in-memory database has nothing to maintain")
        self._engine = engine
        self._files = {"main": Path(database), **queue_files}
        self._clock = clock
        self._window = window
        self._space = space
        self._gate = gate
        self._freeze = freeze if freeze is not None else FreezeState()

    # -- the freeze ----------------------------------------------------------------------------

    def _frozen(self) -> bool:
        try:
            with self._engine.connect() as connection:
                return self._freeze.frozen_in(connection)
        except sa.exc.OperationalError:
            return True  # a locked database is not one to rewrite

    @contextmanager
    def _open(self, name: str, wait_s: float) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._files[name], timeout=wait_s, isolation_level=None)
        try:
            yield connection
        finally:
            connection.close()

    # -- the three upkeeps ---------------------------------------------------------------------

    def optimize(self) -> int:
        """`PRAGMA optimize` on every file. Returns how many were done."""
        done = 0
        for name in self._files:
            with self._open(name, VACUUM_WAIT_S) as connection:
                connection.execute("PRAGMA optimize")
            done += 1
        return done

    def checkpoint(self) -> CheckpointReport:
        if self._frozen():
            return CheckpointReport(frozen=True)
        partial: list[str] = []
        for name in self._files:
            with self._open(name, CHECKPOINT_WAIT_S) as connection:
                busy, _log, _done = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                partial.append(name)
                log.info("sqlite.checkpoint_partial", database=name)
            elif name == "main":
                settle_pending(self._files[name])  # an erasure that waited for the log is done
        return CheckpointReport(len(self._files), partial)

    def _last_vacuum(self) -> datetime | None:
        with self._engine.connect() as connection:
            value = connection.execute(
                sa.select(platform.app_setting.c.value).where(
                    platform.app_setting.c.key == VACUUM_RECORD
                )
            ).scalar()
        return datetime.fromisoformat(value) if value else None

    def _record_vacuum(self, at: datetime) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sqlite_insert(platform.app_setting)
                .values(key=VACUUM_RECORD, value=at.isoformat())
                .on_conflict_do_update(index_elements=["key"], set_={"value": at.isoformat()})
            )

    def vacuum_blocker(self, running_elsewhere: int) -> str | None:
        """Why a VACUUM cannot start now, or None. Order: the freeze, the window, the month, any
        other running job, the room."""
        if self._frozen():
            return "frozen"
        if self._window.span_containing(self._clock.now()) is None:
            return "outside_window"
        last = self._last_vacuum()
        if last is not None and self._clock.now() - last < VACUUM_EVERY:
            return "not_due"
        if running_elsewhere or self._gate.holders():
            return "busy"
        biggest = max(self._size(name) for name in self._files)
        if not self._space.has_room_for_vacuum(database_bytes=biggest, volume="data"):
            return "no_room"
        return None

    def _size(self, name: str) -> int:
        path = self._files[name]
        return sum(p.stat().st_size for p in (path, Path(f"{path}-wal")) if p.exists())

    def vacuum_steps(self) -> list[Callable[[], None]]:
        """One step per file, run in order; the last one writes the monthly record."""

        def one(name: str) -> Callable[[], None]:
            def run() -> None:
                with self._gate.exclusive() as ours:
                    if not ours:
                        raise MaintenanceBusyError("the gate was taken before the VACUUM")
                    with self._open(name, VACUUM_WAIT_S) as connection:
                        connection.execute("VACUUM")
                        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the file shrinks
                log.info("sqlite.vacuumed", database=name)

            return run

        steps = [one(name) for name in self._files]

        def record() -> None:
            self._record_vacuum(self._clock.now())

        return [*steps, record]

    def vacuum(
        self,
        own_job: str | None = None,
        *,
        runner: Callable[[list[Callable[[], None]]], WindowRun] | None = None,
    ) -> VacuumReport:
        """The VACUUM of every file. `runner` runs the steps: the job passes `JobContext.run_steps`,
        which stops between two files when the window closes and resumes the next night; without
        one the steps simply run in order."""
        blocker = self.vacuum_blocker(self.others_running(own_job))
        if blocker is not None:
            return VacuumReport(blocker)
        steps = self.vacuum_steps()
        if runner is None:
            for step in steps:
                step()
            return VacuumReport("done", len(self._files))
        run = runner(steps)
        return VacuumReport("preempted" if run.preempted else "done", len(self._files))

    def others_running(self, own_job: str | None) -> int:
        job = platform.job_view
        statement = sa.select(sa.func.count()).select_from(job).where(job.c.status == "running")
        if own_job is not None:
            statement = statement.where(job.c.id != own_job)
        with self._engine.connect() as connection:
            return int(connection.execute(statement).scalar_one())


def register_sqlite_maintenance(
    pools: JobPools,
    engine: Engine,
    *,
    space: SpaceCheck,
    gate: MaintenanceGate,
    freeze: FreezeState | None = None,
) -> SqliteMaintenance:
    upkeep = SqliteMaintenance(
        engine,
        queue_files=pools.queue_files(),
        clock=pools.clock,
        window=pools.window,
        space=space,
        gate=gate,
        freeze=freeze,
    )

    def optimize(context: JobContext) -> None:
        context.record(databases=upkeep.optimize())

    def checkpoint(context: JobContext) -> None:
        report = upkeep.checkpoint()
        context.record(databases=report.databases, partial=len(report.partial))

    def vacuum(context: JobContext) -> None:
        report = upkeep.vacuum(context.job_id, runner=context.run_steps)
        context.record(outcome=report.outcome)  # a blocked one is tried again tomorrow night

    pools.register(OPTIMIZE_KIND, pool="maint", handler=optimize)
    pools.scheduler.add(OPTIMIZE_KIND, OPTIMIZE_CRON, jitter_s=600)
    pools.register(CHECKPOINT_KIND, pool="maint", handler=checkpoint, windowed=True)
    pools.scheduler.add(CHECKPOINT_KIND, CHECKPOINT_CRON, windowed=True)
    pools.register(VACUUM_KIND, pool="maint", handler=vacuum, windowed=True)
    pools.scheduler.add(VACUUM_KIND, VACUUM_CRON, windowed=True)
    return upkeep
