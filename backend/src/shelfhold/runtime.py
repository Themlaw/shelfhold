"""Start-up of one instance, in the order the spec fixes (ENF-OPS-015, EF-EXP-041, 02 10.2).

0. `/data/.lock` (a second instance on the same volumes refuses to start:
   `instance.lease_conflict`);
1. the open intents are settled, with the roots' write leases taken for the time it takes;
2. only then `boot_jobs` (flush of the Huey locks, domain slots and resource locks, requeue, scan).

Every write under `/library` from then on is refused by `Fsops` without this instance's lease.
"""

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.composition import Composition, Gates, open_fsops
from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.export.application import root_capabilities
from shelfhold.export.application.intents import IntentJournal, OpenIntent, ResolveReport
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.manifest_write import adopt_written_hook
from shelfhold.export.application.restore import RestoredBehindError, RestoreGuard
from shelfhold.export.application.root_bootstrap import (
    RootPolicy,
    register_roots,
    target_roots,
)
from shelfhold.export.application.roots import LoadedRoots, load_roots
from shelfhold.export.application.upgrade import SamePathUpgrade
from shelfhold.export.domain.path_map import PathMap, PathMode
from shelfhold.export.domain.physical_roots import LEGACY_RECEIVED_DIR, RECEIVED_DIR
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.fsops import BootReport, Fsops
from shelfhold.infrastructure.fsops.capabilities import unprobed
from shelfhold.infrastructure.fsops.probe import ProbeReport, probe_failure, probe_folder
from shelfhold.infrastructure.identity_counter import IdentityCounter
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
from shelfhold.infrastructure.wal import settle_pending
from shelfhold.jobs.huey_app import JobPools, boot_jobs
from shelfhold.jobs.maintenance import MaintenanceGate, register_sqlite_maintenance
from shelfhold.jobs.window import MaintenanceWindow
from shelfhold.library.public import (
    MetaProjection,
    complete_interrupted_keep,
    migrate_received_dirs,
)
from shelfhold.ops.backup import BackupService, register_backup_job
from shelfhold.runtime_jobs import access_audit, register_runtime_jobs

RETAKE_INTERVAL_S = 2.0  # how often a process that found `/data/.lock` held looks again


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
    freeze_state: FreezeState = field(default_factory=FreezeState, init=False)  # EF-INT-026
    _recheck: Callable[[], None] | None = field(default=None, init=False)
    composition: Composition | None = field(default=None, init=False)
    startup_probe: list[ProbeReport] = field(default_factory=list, init=False)  # EF-UX-002

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
            structlog.get_logger().warning(
                "instance.waiting_for_lock",
                reason="migration_running",
                detail="no write until the migration ends; the lock is taken again then",
            )
            return
        self.owns_volumes = True

    def try_take_over(self) -> bool:
        """After `acquire` found the lock held by a migration: take it once it is free. False
        while it is still held; True once this process owns the volumes (the caller then runs the
        rest of the start-up)."""
        try:
            self.lock.acquire()
        except InstanceLockHeldError:
            return False
        self.owns_volumes = True
        structlog.get_logger().info("instance.lock_taken")
        return True

    @property
    def restored_behind(self) -> bool:
        """EF-INT-026: read only until `reconcile --adopt-after-restore`."""
        return self.freeze_state.frozen

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
        wanted = target_roots(engine)
        wanted.append((str(self.settings.store_dir), PathMode.RW))
        policy = RootPolicy(
            self.settings.library_dir, self.settings.data_dir, self.settings.store_dir
        )
        for item in wanted:  # one refused or unwritable folder never stops the others
            try:
                register_roots(engine, [item], policy=policy)  # first start of an install
            except (OSError, ValueError, sa.exc.SQLAlchemyError) as error:
                structlog.get_logger().warning("root_bootstrap_skipped", error=str(error))
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
            self.freeze_state.follow(loaded.freeze.generation)  # one state for fsops and jobs
            watcher = composition.root_watcher()
            fsops = open_fsops(
                loaded, umask=self.settings.umask,
                write_gate=guard.require_space, stop_gate=self.barrier.require_open,
                freeze=self.freeze_state, on_blocked=watcher.note_blocked,
            )  # fmt: skip
            journal = IntentJournal(engine, fsops, clock=clock)
            # Before anything else: a database older than the disk is read only (EF-INT-026).
            restore_guard = RestoreGuard(
                engine, fsops, clock=clock, bus=bus,
                counter=IdentityCounter(self.settings.data_dir),
            )  # fmt: skip

            def freeze() -> None:
                status = restore_guard.enter_restored_behind()
                if status.behind:
                    self.freeze_state.freeze()  # the scheduler reads the persisted freeze itself

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
            watcher.bind(self.freeze_state)
            watcher.refresh()  # foreign_root, root_lost, root_behind, store_lost (EF-EXP-064)
            ready = self._probed(fsops, loaded, engine, clock, bus, instance_id)
            sessions = WriteSessions(
                fsops, instance_id=instance_id, clock=clock, lock_path=self.lock.path,
                generations=GenerationCounter(engine), on_behind=freeze,
            )  # fmt: skip
            sessions.enforce()  # no write path skips the lease from here on
            with sessions.open(*ready):
                migrate_received_dirs(
                    engine, fsops,
                    [
                        (r.root_id, r.path)
                        for r in loaded.roots
                        if r.mode is PathMode.RW and r.path in ready
                    ],  # a root that is away or refuses is left for the next start
                    old_dir=LEGACY_RECEIVED_DIR, new_dir=RECEIVED_DIR,
                )  # fmt: skip
                adopt = adopt_written_hook(fsops, loaded.locate, clock)
                upgraded = SamePathUpgrade(  # an upgrade cut short: its bookkeeping (EF-EXP-016)
                    engine, fsops, locate=loaded.locate, data_dir=self.settings.data_dir,
                    clock=clock, bus=bus,
                ).settle_hook()  # fmt: skip

                def hook(connection: sa.Connection, intent: OpenIntent) -> None:
                    adopt(connection, intent)
                    upgraded(connection, intent)
                    if intent.op == "copy_cross_root" and intent.dst is not None:
                        complete_interrupted_keep(  # a kept copy on disk, its bookkeeping owed
                            connection, fsops, loaded.locate, src=intent.src, dst=intent.dst,
                            now=clock.now(),
                        )  # fmt: skip

                result = journal.resolve_open(on_done=hook)
            for intent_id in result.drifted:
                structlog.get_logger().warning(
                    "reconcile.drift", intent=intent_id, reason=result.drift_reasons[intent_id]
                )
                with engine.begin() as conn:  # the log is not enough: the channels are told
                    bus.publish(
                        conn,
                        "reconcile.drift",
                        {"intent_id": intent_id, "kind": "unsettled_intent"},
                    )
            report.append(result)
            return result

        self.pools = JobPools(
            self.settings.data_dir,
            engine=engine,
            clock=clock,
            seed=instance_id or "unregistered",
            window=MaintenanceWindow.from_text(
                self.settings.maintenance_window_start,
                self.settings.maintenance_window_end,
                self.settings.timezone,
            ),
            bus=bus,
            freeze=self.freeze_state,
        )
        watcher = DiskWatcher(guard, self.pools, engine=engine, bus=bus)

        def disk_check() -> None:
            with engine.connect() as conn:  # a root added since start-up is watched too
                roots = (
                    conn.execute(
                        sa.select(platform.physical_root.c.path_entry).where(
                            platform.physical_root.c.mode == PathMode.RW.value
                        )
                    )
                    .scalars()
                    .all()
                )
            guard.follow_roots(roots)
            watcher.check()
            composition.flush_owed_tombstones()  # a root that came back pays what it owes

        # the backup holds it (`backup.run`, and `shelfhold backup run` through the lock file)
        self.maintenance_gate = MaintenanceGate(self.settings.data_dir / ".maintenance.lock")
        register_sqlite_maintenance(
            self.pools, engine, space=guard, gate=self.maintenance_gate, freeze=self.freeze_state
        )
        register_ops_jobs(
            self.pools, self.settings, disk_check=disk_check, audit=access_audit(composition)
        )
        register_runtime_jobs(self.pools, composition)
        backups = BackupService(
            self.settings, engine, clock=clock, bus=composition.bus, gate=self.maintenance_gate
        )
        register_backup_job(self.pools, backups)
        with contextlib.suppress(OSError):  # a run a kill cut short, and its clear snapshot
            backups.settle_interrupted()
        boot_jobs(
            self.pools,
            instance_lock=self.lock,
            resolve_intents=resolve,
            release_resource_locks=ResourceLocks(engine, clock=clock).release_all,
        )
        watcher.check()  # the first measure at once: a full disk parks the jobs boot just sent
        self.pools.start_workers()  # the queues are consumed from here on
        return report[0] if report else None

    def start_projection_repair(self) -> None:
        """`unit.json` and `work.json` are projections of the database, written after the
        transaction: a crash in between left one missing or torn. They are written again at
        start (ENF-OPS-020), under `/data` only, on a thread of their own once the server is
        listening: a big catalog must not delay `/readyz` (ENF-PERF-003), and the descriptors of
        the socket being bound are not to be shared with a thread that opens files meanwhile."""
        if self.engine is None or not self.owns_volumes:
            return
        threading.Thread(
            target=self._repair_projections, args=(self.engine,), name="meta-repair", daemon=True
        ).start()
        # an erasure that waited for a reader to leave the log: done at start, if it can be
        threading.Thread(
            target=settle_pending,
            args=(self.settings.database_path,),
            name="wal-settle",
            daemon=True,
        ).start()

    def _repair_projections(self, engine: Engine) -> None:
        log = structlog.get_logger()
        try:
            with engine.connect() as connection:
                if self.freeze_state.frozen_in(connection):
                    log.warning("meta_repair_skipped", reason="restored_behind")  # no write
                    return
            report = MetaProjection(engine, self.settings.data_dir).repair()
        except (OSError, ValueError, sa.exc.SQLAlchemyError) as error:
            log.warning("meta_repair_skipped", error=type(error).__name__)  # the next start tries
            return
        for kind, found in (("unit", report.read_only), ("work", report.works_read_only)):
            if found:  # a newer format is never rewritten (ENF-OPS-013)
                log.warning("reconcile.drift", kind=f"{kind}_json_newer_format", count=len(found))

    def _probed(
        self,
        fsops: Fsops,
        loaded: LoadedRoots,
        engine: Engine,
        clock: SystemClock,
        bus: EventBus,
        instance_id: str | None,
    ) -> list[str]:
        """The roots the instance writes to, each probed once at start (EF-UX-002): a folder the
        container's uid cannot write is reported with its cause and the compose block that fixes
        it, announced (`target.permission_denied`), and left out of the write session, so one
        wrong uid on a mount does not stop the others, nor the start."""
        booted = fsops.boot()
        ready = list(booted.ok)
        sessions = WriteSessions(
            fsops, instance_id=instance_id or "", clock=clock, lock_path=self.lock.path
        )  # no generation counter: the probe moves nothing of the restore epoch
        self.startup_probe = []
        writable = {r.path for r in loaded.roots if r.mode is PathMode.RW}
        for path in [p for p in ready if p in writable]:
            report = self._probe_root(fsops, sessions, path)
            self.startup_probe.append(report)
            if report.ok:
                continue
            ready.remove(path)
            structlog.get_logger().error(
                "startup.probe_failed", folder=path, cause=report.cause, step=report.failed_step,
                yaml_hint=report.yaml_hint,
            )  # fmt: skip
            if report.event is not None:
                with engine.begin() as conn:
                    bus.publish(conn, report.event, {"reason": "forbidden"})
        self._probe_capabilities(fsops, sessions, loaded, engine, ready, booted)
        return ready

    def _probe_capabilities(
        self,
        fsops: Fsops,
        sessions: WriteSessions,
        loaded: LoadedRoots,
        engine: Engine,
        ready: list[str],
        booted: BootReport,
    ) -> None:
        """Reflink, link to `/store` and inode stability of each writable root (EF-EXP-059),
        probed once at start, removed at once, and written down for `shelfhold status`. A root
        that cannot be probed is simply not listed: the start goes on."""
        store = str(self.settings.store_dir)
        for root in loaded.roots:
            if root.mode is not PathMode.RW:
                continue
            if root.path not in ready:  # said, not hidden: `status` shows why it was not probed
                why = (
                    "tombstone_lines_owed" if root.path in booted.tombstone_owed
                    else booted.blocked[root.path].value if root.path in booted.blocked
                    else "write_probe_failed"
                )  # fmt: skip
                with engine.begin() as conn:
                    root_capabilities.save(conn, root.root_id, unprobed(root.path, why))
                continue
            leased = [root.path, *([store] if store in ready and store != root.path else [])]
            try:
                with sessions.open(*leased):
                    found = fsops.probe_capabilities(root.path, store_path=store)
                with engine.begin() as conn:
                    root_capabilities.save(conn, root.root_id, found)
            except (OSError, sa.exc.SQLAlchemyError) as error:
                structlog.get_logger().warning(
                    "startup.capabilities_skipped", folder=root.path, error=str(error)
                )

    @staticmethod
    def _probe_root(fsops: Fsops, sessions: WriteSessions, path: str) -> ProbeReport:
        try:
            with sessions.open(path):
                return probe_folder(fsops, path)  # type: ignore[arg-type]
        except OSError as error:  # the lease itself cannot be written there
            return probe_failure(path, error, step="lease")

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
