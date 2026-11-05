"""Start-up of one instance, in the order the spec fixes (ENF-OPS-015, EF-EXP-041, 02 10.2).

0. `/data/.lock` (a second instance on the same volumes refuses to start:
   `instance.lease_conflict`);
1. the open intents are settled, with the roots' write leases taken for the time it takes;
2. only then `boot_jobs` (flush of the Huey locks, domain slots and resource locks, requeue, scan).

Every write under `/library` from then on is refused by `Fsops` without this instance's lease.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.composition import Composition, Gates
from shelfhold.config import Settings
from shelfhold.export.application.intents import IntentJournal, ResolveReport
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.manifest_write import adopt_written_hook
from shelfhold.export.application.restore import RestoredBehindError, RestoreGuard
from shelfhold.export.application.roots import load_roots
from shelfhold.export.domain.path_map import PathMap
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.instance import (
    GenerationCounter,
    InstanceLock,
    InstanceLockHeldError,
    WriteSessions,
)
from shelfhold.infrastructure.migrate import MigrationStateFile, current_revision, head_revision
from shelfhold.infrastructure.observability.disk_guard import DiskGuard
from shelfhold.infrastructure.observability.disk_watch import DiskWatcher
from shelfhold.infrastructure.observability.shutdown import (
    GracefulShutdown,
    ShutdownReport,
    WriteBarrier,
)
from shelfhold.infrastructure.observability.wiring import disk_guard_for, register_ops_jobs
from shelfhold.infrastructure.schema import platform
from shelfhold.jobs.huey_app import JobPools, boot_jobs
from shelfhold.runtime_jobs import access_audit, register_runtime_jobs


@dataclass
class InstanceRuntime:
    settings: Settings
    lock: InstanceLock = field(init=False)
    engine: Engine | None = field(default=None, init=False)
    pools: JobPools | None = field(default=None, init=False)
    owns_volumes: bool = field(default=False, init=False)
    disk_guard: DiskGuard | None = field(default=None, init=False)
    shutdown_report: ShutdownReport | None = field(default=None, init=False)
    barrier: WriteBarrier = field(default_factory=WriteBarrier, init=False)
    restored_behind: bool = field(default=False, init=False)  # EF-INT-026: read only
    _recheck: Callable[[], None] | None = field(default=None, init=False)
    composition: Composition | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.lock = InstanceLock(self.settings.data_dir)

    def acquire(self) -> None:
        """Step 0. A held lock refuses the start, except while a migration holds it: the server
        then only answers `/healthz` (migrating) and writes nothing, jobs included."""
        try:
            self.lock.acquire()
        except InstanceLockHeldError:
            if MigrationStateFile(self.settings.data_dir).read() is None:
                raise
            structlog.get_logger().warning("instance_lock_held_by_migration_no_write_mode")
            return
        self.owns_volumes = True

    def require_writable(self) -> None:
        """The gate of every write the application starts: refused while `restored_behind`."""
        if not self.restored_behind and self._recheck is not None:
            self._recheck()  # a root that came back after the start may be ahead (EF-INT-026)
        if self.restored_behind:
            raise RestoredBehindError(
                "restored_behind: run `shelfhold reconcile --adopt-after-restore`"
            )

    def boot(self) -> ResolveReport | None:
        """Steps 1 and 2; nothing to do before the database exists at the current schema."""
        if not self.owns_volumes:
            return None
        database = self.settings.database_path
        if current_revision(database) != head_revision():
            return None  # `shelfhold migrate` first
        clock = SystemClock()
        engine = create_db_engine(database)
        self.engine = engine
        try:
            return self._boot(engine, clock)
        except sa.exc.OperationalError as error:
            # Read-only or locked database: no job starts; `/readyz` reports the state.
            structlog.get_logger().error("boot_skipped_database_unavailable", error=str(error))
            return None

    def gates(self) -> Gates:
        """What this runtime knows that the services built outside it need to honour."""
        return Gates(
            write_gate=self.require_space, stop_gate=self.barrier.require_open,
            writable=self.require_writable, lock_path=self.lock.path, on_behind=self.on_behind,
        )  # fmt: skip

    def on_behind(self) -> None:
        if self._recheck is not None:
            self._recheck()  # a root remounted ahead of the database: read only from now on

    def _boot(self, engine: Engine, clock: SystemClock) -> ResolveReport | None:
        composition = self.composition or Composition(
            self.settings, engine, clock=clock, gates=self.gates()
        )  # the server hands over its own: one lease identity per process
        self.composition = composition
        bus = composition.bus  # an operating event queues one notification per channel
        with engine.connect() as conn:
            row = conn.execute(sa.select(platform.instance.c.instance_id)).first()
            paths = [r.path_entry for r in conn.execute(sa.select(platform.physical_root))]
            instance_id = row.instance_id if row else None
            loaded = load_roots(conn, PathMap.same_path(paths)) if row else None
        report: list[ResolveReport] = []
        guard = disk_guard_for(
            self.settings, [root.path for root in loaded.expected] if loaded else ()
        )
        self.disk_guard = guard

        def resolve() -> ResolveReport:
            if loaded is None or instance_id is None:
                return ResolveReport()  # no instance row yet: nothing was ever written
            fsops = Fsops.open(
                loaded.path_map, loaded.expected, write_gate=guard.require_space,
                stop_gate=self.barrier.require_open, frozen=loaded.restore_pending,
            )  # fmt: skip
            journal = IntentJournal(engine, fsops, clock=clock)
            # Before anything else: a database older than the disk is read only (EF-INT-026).
            restore_guard = RestoreGuard(engine, fsops, clock=clock, bus=bus)

            def freeze() -> None:
                status = restore_guard.enter_restored_behind()
                if status.behind:
                    self.restored_behind = True
                    if self.pools is not None:
                        self.pools.scheduler.hold()

            fsops.on_restored_behind(freeze)
            self._recheck = freeze
            status = restore_guard.enter_restored_behind()
            if status.behind:
                freeze()
                structlog.get_logger().error("instance.restored_behind", epoch=status.epoch)
                # no session, no lease, no write: the intents of the backup become stale and
                # nothing is replayed (the thaw is `reconcile --adopt-after-restore`)
                result = journal.resolve_open()
                report.append(result)
                return result
            ready = fsops.boot().ok
            sessions = WriteSessions(
                fsops, instance_id=instance_id, clock=clock, lock_path=self.lock.path,
                generations=GenerationCounter(engine), on_behind=freeze,
            )  # fmt: skip
            sessions.enforce()  # no write path skips the lease from here on
            with sessions.open(*ready):
                hook = adopt_written_hook(fsops, loaded.locate, clock)
                result = journal.resolve_open(on_done=hook)
            for intent_id in result.drifted:
                structlog.get_logger().warning(
                    "reconcile.drift", intent=intent_id, reason=result.drift_reasons[intent_id]
                )
            report.append(result)
            return result

        self.pools = JobPools(
            self.settings.data_dir, engine=engine, clock=clock, seed=instance_id or "unregistered"
        )
        watcher = DiskWatcher(guard, self.pools, engine=engine, bus=bus)
        register_ops_jobs(
            self.pools, self.settings, disk_check=watcher.check, audit=access_audit(composition)
        )
        register_runtime_jobs(self.pools, composition)
        boot_jobs(
            self.pools,
            instance_lock=self.lock,
            resolve_intents=resolve,
            release_resource_locks=ResourceLocks(engine, clock=clock).release_all,
        )
        watcher.check()  # the first measure at once: a full disk parks the jobs boot just sent
        self.pools.start_workers()  # the queues are consumed from here on
        return report[0] if report else None

    def require_space(self) -> None:
        """The write gate handed to every `Fsops` opened by this process."""
        if self.disk_guard is not None:
            self.disk_guard.require_space()

    def shutdown(self, *, checkpoint_s: float | None = None) -> ShutdownReport | None:
        """SIGTERM, in three steps (ENF-OPS-007); idempotent. `stop` then closes the database."""
        if self.pools is None or not self.owns_volumes:
            return None
        engine = self.engine
        locks = ResourceLocks(engine, clock=SystemClock()) if engine is not None else None
        options = {} if checkpoint_s is None else {"checkpoint_s": checkpoint_s}
        self.shutdown_report = GracefulShutdown(
            self.pools,
            instance_lock=self.lock,
            release_resource_locks=locks.release_all if locks else None,
            barrier=self.barrier,
            **options,
        ).run()
        return self.shutdown_report

    def stop(self) -> None:
        if self.pools is not None:
            self.pools.stop_workers()
            for name in self.pools.names:  # Huey's SQLite connections are file descriptors
                self.pools.huey(name).storage.close()
            self.pools = None
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None
        self.lock.release()
        self.owns_volumes = False
