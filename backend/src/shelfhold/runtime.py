"""Start-up of one instance, in the order the spec fixes (ENF-OPS-015, EF-EXP-041, 02 10.2).

0. `/data/.lock` (a second instance on the same volumes refuses to start:
   `instance.lease_conflict`);
1. the open intents are settled, with the roots' write leases taken for the time it takes;
2. only then `boot_jobs` (flush of the Huey locks, domain slots and resource locks, requeue, scan).

Every write under `/library` from then on is refused by `Fsops` without this instance's lease.
"""

from dataclasses import dataclass, field

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.export.application.intents import IntentJournal, ResolveReport
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.manifest_write import adopt_written_hook
from shelfhold.export.application.roots import load_roots
from shelfhold.export.domain.path_map import PathMap
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.instance import InstanceLock, InstanceLockHeldError, WriteSessions
from shelfhold.infrastructure.migrate import MigrationStateFile, current_revision, head_revision
from shelfhold.infrastructure.schema import platform
from shelfhold.jobs.huey_app import JobPools, boot_jobs


@dataclass
class InstanceRuntime:
    settings: Settings
    lock: InstanceLock = field(init=False)
    engine: Engine | None = field(default=None, init=False)
    pools: JobPools | None = field(default=None, init=False)
    owns_volumes: bool = field(default=False, init=False)

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

    def _boot(self, engine: Engine, clock: SystemClock) -> ResolveReport | None:
        with engine.connect() as conn:
            row = conn.execute(sa.select(platform.instance.c.instance_id)).first()
            paths = [r.path_entry for r in conn.execute(sa.select(platform.physical_root))]
            instance_id = row.instance_id if row else None
            loaded = load_roots(conn, PathMap.same_path(paths)) if row else None
        report: list[ResolveReport] = []

        def resolve() -> ResolveReport:
            if loaded is None or instance_id is None:
                return ResolveReport()  # no instance row yet: nothing was ever written
            fsops = Fsops.open(loaded.path_map, loaded.expected)
            ready = fsops.boot().ok
            sessions = WriteSessions(
                fsops, instance_id=instance_id, clock=clock, lock_path=self.lock.path
            )
            sessions.enforce()  # no write path skips the lease from here on
            journal = IntentJournal(engine, fsops, clock=clock)
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
        boot_jobs(
            self.pools,
            instance_lock=self.lock,
            resolve_intents=resolve,
            release_resource_locks=ResourceLocks(engine, clock=clock).release_all,
        )
        return report[0] if report else None

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None
        self.lock.release()
        self.owns_volumes = False
