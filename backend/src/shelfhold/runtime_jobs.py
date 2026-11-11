"""The periodic jobs of the runtime, registered before `boot_jobs`' scan (PLAN-M1a section 7).

- `follow.cycle` (L18): the always-on cycle of the connected service, its archives received into
  the inbox of the first writable root, under the lease, with the tombstone gate;
- `export.run` (L27, L50): per destination, the plan from the database alone; only a plan with
  something to write goes on to the foreign-writer detector, the lease, the writing and the
  targeted scans; then the arrival poll and the series covers (EF-EXP-034, EF-EXP-045). An empty
  plan touches no disk (ENF-OPS-014);
- `export.apply` (L51): the application of a stored plan asked for by `POST /plans/{id}/apply`,
  which answers 202 and the URL of this job; the names are read from Kavita again and must be
  the ones of the preview before anything is written (EF-INT-001, EF-EXP-045);
- `reconcile.cycle` and `reconcile.hash` (L32, L50): the verification of the tree, both window
  jobs (ENF-OPS-014): by day nothing looks at the disks, the second one may hash files;
- `target.access_audit` (L26, L37) and `ops.disk_check` (L37): see `infrastructure.observability`;
- `target.key_expiry` (daily): the J-14/J-1 notices of a Kavita key, read again from the target;
- `intents.prune` (daily): the closed intents older than the retention leave the journal;
- `notify.dispatch` (EF-INT-006): the sender of the notification queue.

A job that finds nothing to do (no Suwayomi, no shelf, no keyring yet) returns quietly: a fresh
install runs every job and none of them touches anything.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import sqlalchemy as sa
import structlog
from structlog.contextvars import bound_contextvars

from shelfhold.acquisition.public import (
    FOLLOW_JOB_KIND,
    FollowCycle,
    LeasedReceiver,
    bounded_receiver,
)
from shelfhold.composition import (
    KAVITA_WAKE,
    Composition,
    KeyringUnavailableError,
    NoLibraryError,
    WriteContext,
)
from shelfhold.export.application import blocked_memo
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import work_lock_for
from shelfhold.export.application.migration_detect import SwitchoverDetector, SwitchoverGate
from shelfhold.export.application.pipeline import (
    ExportPipeline,
    PlanPreview,
    SourceMissingError,
    UnverifiedParseError,
    ensure_applicable,
    wanted_names,
)
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.preflight import PermissionPreflight
from shelfhold.export.application.reconcile import Reconciler
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.application.waiting_marks import mark_mount_blocked
from shelfhold.export.application.wiring import NoRemoteParse
from shelfhold.export.domain.path_map import PathMapError
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.export.targets.kavita.adopt import BulkParse, library_type_named
from shelfhold.export.targets.kavita.arrival import ArrivalPoller
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.cover import SeriesCovers
from shelfhold.export.targets.kavita.errors import (
    RouteMissing,
    TargetError,
    TargetForbidden,
    TargetUnreachable,
)
from shelfhold.export.targets.kavita.series import SeriesAges, refresh_foreign_names, refresh_names
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.observability.wiring import daily_access_audit
from shelfhold.infrastructure.schema import coherence, export
from shelfhold.infrastructure.wake import WakeDeferred
from shelfhold.integrations.apprise import NotificationDispatcher, register_notification_job
from shelfhold.jobs.public import JobContext, JobFailedError, JobPools
from shelfhold.library.public import PassthroughStore, RangedKeeper, UntrustedEpubStore
from shelfhold.ops.errors import ExitUnavailableError
from shelfhold.ops.events import BusTargetEvents
from shelfhold.retention_jobs import register_retention_jobs
from shelfhold.safeguard_jobs import register_safeguard_job
from shelfhold.store_jobs import register_store_jobs
from shelfhold.work_locks import work_holder

__all__ = [
    "APPLY_JOB_KIND",
    "EXPORT_JOB_KIND",
    "INTENT_PRUNE_KIND",
    "KEY_EXPIRY_KIND",
    "RECONCILE_HASH_KIND",
    "RECONCILE_KIND",
    "PlanService",
    "register_runtime_jobs",
    "settle_arrival",
]

log = structlog.get_logger(__name__)

EXPORT_JOB_KIND = "export.run"
APPLY_JOB_KIND = "export.apply"
RECONCILE_KIND = "reconcile.cycle"
RECONCILE_HASH_KIND = "reconcile.hash"
FOLLOW_CRON = "*/15 * * * *"
EXPORT_CRON = "*/5 * * * *"
RECONCILE_CRON = "*/10 * * * *"  # window job: runs every 10 minutes of the window only
INTENT_PRUNE_KIND = "intents.prune"
KEY_EXPIRY_KIND = "target.key_expiry"
INTENT_PRUNE_CRON = "41 3 * * *"
KEY_EXPIRY_CRON = "23 6 * * *"
INTENT_RETENTION = timedelta(days=30)  # a closed intent is kept a month for the audit trail
RECONCILE_HASH_CRON = "30 3 * * *"  # inside the maintenance window, where files may be hashed
SCAN_QUIET_S = 31  # the debounce of the scans (30 s of calm per destination)


@dataclass(frozen=True)
class Destination:
    id: str
    target_id: str


def tree_destinations(comp: Composition) -> list[Destination]:
    table = export.shelf_destination
    with comp.engine.connect() as connection:
        rows = connection.execute(
            sa.select(table.c.id, table.c.target_id)
            .where(table.c.transport == "tree")
            .order_by(table.c.id)
        )
        return [Destination(r.id, r.target_id) for r in rows]


# -- follow.cycle -----------------------------------------------------------------


class _LazyWrite:
    """The write context of a follow cycle, opened by the first thing that needs it.

    A cycle that finds nothing to file reads the database and the service, and nothing else:
    the lease on `/library` (and the sentinel, the tombstone journal, the inbox) is only taken
    when a chapter is about to be received, so the 15-minute cron wakes no disk (ENF-OPS-014)."""

    def __init__(self, comp: Composition, stack: ExitStack) -> None:
        self._comp = comp
        self._stack = stack
        self._context: WriteContext | None = None

    def get(self) -> WriteContext:
        if self._context is None:
            self._context = self._stack.enter_context(self._comp.write_context())
        return self._context


class _LazyTombstones:
    """The tombstone checks of the cycle (a read of the database: no lease), on the service that
    is replayed once per process and found again here, opened at the first check."""

    def __init__(self, comp: Composition) -> None:
        self._comp = comp
        self._service: Any = None

    def __getattr__(self, name: str) -> Any:
        if self._service is None:
            self._service = self._comp.read_context()[2]
        return getattr(self._service, name)


class _LazyStore:
    def __init__(self, comp: Composition, lazy: _LazyWrite) -> None:
        self._comp = comp
        self._lazy = lazy

    def ingest(self, **arguments: Any) -> Any:
        w = self._lazy.get()
        if str(arguments.get("dest", "")).lower().endswith(".epub"):  # EF-BIB-051: neutralized
            store_dir = str(self._comp.settings.store_dir)
            mounted = any(e.path == store_dir for e in w.loaded.expected)
            epubs = UntrustedEpubStore(
                self._comp.engine, w.fsops, w.loaded.locate, self._comp.settings.data_dir,
                store_dir=store_dir if mounted else None, tombstones=w.tombstones,
                hold_work=work_lock_for(self._comp.engine, clock=self._comp.clock),
            )  # fmt: skip
            return epubs.ingest(**arguments)
        store = PassthroughStore(
            self._comp.engine, w.fsops, w.loaded.locate, self._comp.settings.data_dir,
            tombstones=w.tombstones, hold_work=work_holder(self._comp),
        )  # fmt: skip
        return store.ingest(**arguments)


def follow_handler(comp: Composition, pools: JobPools) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        arrived: list[int] = []
        link = comp.suwayomi()
        if link is None:
            log.debug("follow.skipped", reason="no_connected_service")
            context.record(skipped="no_connected_service")
            return  # no connected service configured yet
        try:
            with ExitStack() as stack:
                lazy = _LazyWrite(comp, stack)

                def place(_unit: str, variant_id: str, _filename: str) -> str:
                    return lazy.get().received_path(variant_id)

                keeper = RangedKeeper(comp.settings.data_dir)
                keeper.sweep(comp.engine)  # files of a transaction that did not commit
                cycle = FollowCycle(
                    comp.engine, comp.bus, comp.clock, link.service, link.fetcher,
                    store=cast("PassthroughStore", _LazyStore(comp, lazy)),
                    receive=LeasedReceiver(
                        bounded_receiver(
                            link.guarded, staging_dir=comp.settings.data_dir / "tmp",
                            max_bytes=comp.settings.received_cbz_max_bytes,
                            authorize=getattr(link.fetcher, "authorize", None),  # the archive
                        ),  # asks for the same credential as the other routes of the service
                        lazy.get,  # the lease first: nothing is downloaded that cannot be filed
                    ),
                    place=place, slots=pools.slots,
                    keep_range=keeper,  # EF-SRC-034
                    tombstones=cast("Any", _LazyTombstones(comp)),
                    wakes=comp.wakes(),
                    check_mode="piloted" if comp.check_mode() == "piloted" else "always_on",
                )  # fmt: skip

                async def once() -> None:
                    try:
                        report = await cycle.run()
                        context.record(**report.counters())
                        arrived.append(report.fetched)
                    finally:
                        await link.guarded.aclose()

                asyncio.run(once())
        except (NoLibraryError, KeyringUnavailableError) as error:
            log.info("follow.cycle_skipped", reason=type(error).__name__)
            context.record(skipped=type(error).__name__)
            return
        if any(arrived):  # what arrived goes out; a quiet cycle asks for nothing (the cron does)
            pools.submit(EXPORT_JOB_KIND, dedupe_key=EXPORT_JOB_KIND)

    return run


# -- export.run -----------------------------------------------------------------


class ScanRelay:
    """`request_scan` of the pipeline, forwarded to the client of the destination being exported."""

    def __init__(self) -> None:
        self.client: KavitaClient | None = None
        self.asked: list[str] = []

    def __call__(self, remote_path: str) -> None:
        if self.client is not None:
            self.client.request_scan(remote_path)
            self.asked.append(remote_path)


def build_pipeline(
    comp: Composition, scans: ScanRelay | None = None
) -> tuple[ExportPipeline, Reconciler, Any]:
    fsops, loaded, _tombstones = comp.read_context()
    reconciler = Reconciler(comp.engine, fsops, clock=comp.clock, bus=comp.bus)
    pipeline = ExportPipeline(
        comp.engine, fsops, locate=loaded.locate, data_dir=comp.settings.data_dir,
        clock=comp.clock, parse=_unchecked, parse_verified=False,
        segment_policy_confirmed=comp.settings.naming_segment_policy_confirmed,
        sessions=comp.session_opener(fsops, loaded), request_scan=scans,
        detector=lambda destination_id: reconciler.reconcile(destination_id),  # EF-EXP-043
        gate=comp.gates.writable, operating_bus=comp.bus,
    )  # fmt: skip
    pipeline.use_preflight(PermissionPreflight(comp.engine, fsops, comp.bus, clock=comp.clock))
    pipeline.use_switchover(
        SwitchoverGate(comp.engine, SwitchoverDetector(comp.engine, fsops), clock=comp.clock)
    )
    return pipeline, reconciler, fsops


def _unchecked(_name: str) -> Any:
    raise KeyError("names are not checked by plugin/parse")


def _no_remote_parse(_name: str) -> Any:
    """The planner can read a plan whose names Kavita did not read; applying it is refused."""
    return NoRemoteParse()


class _Disks:
    """The pipeline and the file system, built when a destination first needs them: a run that
    finds nothing to write never opens a root (ENF-OPS-014)."""

    def __init__(self, comp: Composition, scans: ScanRelay) -> None:
        self._comp = comp
        self._scans = scans
        self._built: tuple[ExportPipeline, Fsops] | None = None

    def get(self) -> tuple[ExportPipeline, Fsops]:
        if self._built is None:
            pipeline, _reconciler, fsops = build_pipeline(self._comp, self._scans)
            self._built = (pipeline, fsops)
        return self._built

    def fsops(self) -> Fsops:
        return self.get()[1]


def export_handler(comp: Composition) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        destinations = tree_destinations(comp)
        context.record(destinations=len(destinations))
        if not destinations:
            return
        scans = ScanRelay()
        disks = _Disks(comp, scans)
        for destination in destinations:
            if context.stop_requested:
                return
            try:
                client = comp.kavita_client(destination.target_id)
            except (KeyringUnavailableError, KeyError, sa.exc.NoResultFound):
                log.debug("export.destination_skipped", target_id=destination.target_id,
                          reason="no_sealed_key")  # fmt: skip
                continue  # the target has no sealed key yet
            scans.client, scans.asked = client, []
            try:
                context.add(
                    ops_applied=asyncio.run(
                        _export_one(comp, disks, client, destination, scans, context)
                    )
                )
            except (NoLibraryError, KeyringUnavailableError) as error:
                log.info("export.run_skipped", reason=type(error).__name__)
                return

    return run


def _names_may_refresh(comp: Composition, destination_id: str) -> bool:
    """The cache of the names is a write: not while the instance is restored behind, nor for a
    destination that is paused or waits for a human (the export would refuse it as well)."""
    from shelfhold.export.application.target_state import DestinationPausedError, ensure_active

    try:
        if comp.gates.writable is not None:
            comp.gates.writable()
        with comp.engine.connect() as connection:
            ensure_active(connection, destination_id)
    except (DestinationPausedError, RestoredBehindError):
        return False
    return True


async def _probe_version(comp: Composition, client: KavitaClient, target_id: str) -> None:
    """The version Kavita answers now, kept for the barrier of the shelves below 18+: it reads the
    database, not the client, and only this probe and the audit refresh it. A server that does not
    answer leaves the old value, and `blocks_family_shelf` refuses what nobody ever read."""
    from shelfhold.export.application.target_state import load_health, save_health

    await client.probe()
    with comp.engine.begin() as connection:
        health = load_health(connection, target_id)
        health.version, health.version_mode = client.health.version, client.health.version_mode
        save_health(connection, target_id, health)


async def _export_one(
    comp: Composition,
    disks: _Disks,
    client: KavitaClient,
    destination: Destination,
    scans: ScanRelay,
    context: JobContext,
    apply: tuple[str, str] | None = None,
) -> int:
    """`apply`: a stored plan and the validator of its preview, instead of a plan made now.
    Returns the number of operations applied (for the counters of `job.finished`)."""

    with bound_contextvars(target_id=destination.target_id, destination_id=destination.id):
        try:
            # a Kavita behind Sablier is woken first and its session kept open for the whole run
            async with comp.wakes().session(KAVITA_WAKE):
                return await _export_bound(comp, disks, client, destination, scans, context, apply)
        except WakeDeferred:  # not woken in time: the batch is put off, nothing is written
            if apply is not None:
                raise JobFailedError("wake.deferred") from None
            log.info("export.destination_waits", destination_id=destination.id,
                     reason="wake_deferred")  # fmt: skip
            return 0


async def _export_bound(
    comp: Composition,
    disks: _Disks,
    client: KavitaClient,
    destination: Destination,
    scans: ScanRelay,
    context: JobContext,
    apply: tuple[str, str] | None,
) -> int:
    from shelfhold.export.application.target_state import DestinationPausedError

    applied = 0
    try:
        await _probe_version(comp, client, destination.target_id)  # the barrier reads it
        # the names Kavita shows now (it may rename a series, EF-EXP-057), before any collision
        if _names_may_refresh(comp, destination.id):
            await refresh_names(comp.engine, client, destination.id)
        await refresh_foreign_names(comp.engine, client, destination.id)
        # the plan is read from the database: with nothing to write no disk is looked at
        names, kind = wanted_names(
            comp.engine, destination.id,
            segment_policy_confirmed=comp.settings.naming_segment_policy_confirmed,
        )  # fmt: skip
        if names or apply is not None:
            pipeline, _fsops = disks.get()
            bulk = await BulkParse.fetch(client, names, library_type_named(kind))
            pipeline.bind_parse(bulk, verified=True)
            if apply is None:
                report = pipeline.run(destination.id, job_id=context.job_id)
            else:
                plan_id, version = apply
                pipeline.require_names_checked(plan_id)
                report = pipeline.apply(plan_id, if_match=version, job_id=context.job_id)
            applied = len(report.ops)
    except RootBlockedError as error:  # the mount is not the one Shelfhold wrote (EF-EXP-029)
        if error.state is not RootState.RESTORED_BEHIND:  # that one is `restored_behind`
            mark_mount_blocked(comp.engine, destination.id, "sentinel")
        log.info("export.root_blocked", destination_id=destination.id, root=error.root_path)
        if apply is not None:
            raise JobFailedError("root.blocked") from None
        return 0
    except (DestinationPausedError, TargetError) as error:
        if apply is not None:  # asked for by a person: the job says why it did not go through
            raise JobFailedError(_code_of(error)) from None
        log.info(
            "export.destination_waits", destination_id=destination.id, reason=type(error).__name__
        )
        return 0
    finally:
        if scans.asked:  # the debounce: scans leave after a quiet moment
            deadline = time.monotonic() + SCAN_QUIET_S
            while time.monotonic() < deadline and not context.stop_requested:
                await asyncio.sleep(1)
            try:
                await client.flush_scans(_scan_guard(comp, disks, destination.id))
            except TargetError as error:  # Kavita lost after the writing: the next run scans again
                log.info("export.scan_failed", destination_id=destination.id,
                         reason=type(error).__name__)  # fmt: skip
    await settle_arrival(comp, client, destination.id, fsops=disks.fsops)
    return applied


def _scan_guard(comp: Composition, disks: _Disks, destination_id: str) -> Callable[[str], bool]:
    """`allowed(remote_path)` for `flush_scans`: the sentinel of the root under the folder must
    allow a scan (EF-EXP-005, `require_scan_allowed`), else Kavita would see an unmounted
    folder as emptied and drop its series. A folder the destination's path map cannot place is
    not scanned."""

    def allowed(remote: str) -> bool:
        try:
            with comp.engine.connect() as connection:
                ctx = load_context(connection, destination_id)
            disks.fsops().require_scan_allowed(ctx.path_map.to_local(remote))
        except (RootBlockedError, PathMapError, OSError, LookupError):
            log.info("export.scan_refused", destination_id=destination_id, reason="root_not_ready")
            return False
        return True

    return allowed


def _code_of(error: Exception) -> str:
    """The catalog code of a refusal; a failure of the Kavita client says which one it is."""
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    for kind, named in (
        (TargetUnreachable, "target.unreachable"),
        (TargetForbidden, "target.forbidden"),
        (RouteMissing, "target.route_missing"),
    ):
        if isinstance(error, kind):
            return named
    return "job.failed"


async def settle_arrival(
    comp: Composition,
    client: KavitaClient,
    destination_id: str,
    *,
    fsops: Callable[[], Fsops] | None = None,
) -> None:
    """After an export: confirm what Kavita indexed (EF-EXP-034), post the covers of the
    series it now knows (EF-EXP-045), then push their classes (EF-EXP-040). All are read from
    the database first, so with nothing pending neither the network nor a disk is touched beyond
    that."""
    events = BusTargetEvents(comp.engine, comp.bus)

    def disks() -> Fsops:
        return fsops() if fsops is not None else comp.read_context()[0]

    poller = ArrivalPoller(comp.engine, comp.clock, events, fsops=disks)
    try:
        await poller.poll(client, destination_id)
    except TargetError as error:  # one destination failing never stops the others
        log.info("export.poll_failed", destination_id=destination_id, reason=type(error).__name__)
        return

    covers = SeriesCovers(comp.engine, comp.clock, disks, events)
    try:
        await covers.publish(client, destination_id)
    except (NoLibraryError, KeyringUnavailableError) as error:
        log.info("export.cover_skipped", reason=type(error).__name__)
    # the class of the series, locked, by the API: never a rewrite of a file (EF-EXP-040)
    await SeriesAges(comp.engine, comp.clock, events).push(client, destination_id)


# -- export.apply ---------------------------------------------------------------


def apply_key(plan_id: str, if_match: str) -> str:
    """The dedupe key of the job: the plan and the validator of its preview. Two requests for the
    same plan share one job while it is active; a plan applied once is `done` and refused."""
    return f"{APPLY_JOB_KIND}:{plan_id}:{if_match}"


def _plan_of(dedupe_key: str) -> tuple[str, str]:
    _kind, plan_id, version = dedupe_key.split(":", 2)
    return plan_id, version


def _destination_of(comp: Composition, destination_id: str) -> Destination:
    table = export.shelf_destination
    with comp.engine.connect() as connection:
        target_id = connection.execute(
            sa.select(table.c.target_id).where(table.c.id == destination_id)
        ).scalar_one()
    return Destination(destination_id, target_id)


def apply_handler(comp: Composition) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        plan_id, version = _plan_of(context.dedupe_key)
        plan = export_plan_destination(comp, plan_id)
        destination = _destination_of(comp, plan)
        scans = ScanRelay()
        disks = _Disks(comp, scans)
        try:
            client = comp.kavita_client(destination.target_id)
        except (KeyringUnavailableError, KeyError, sa.exc.NoResultFound):
            raise JobFailedError("target.route_missing") from None  # no sealed key: nothing to ask
        scans.client = client
        try:
            context.add(
                ops_applied=asyncio.run(
                    _export_one(
                        comp, disks, client, destination, scans, context, (plan_id, version)
                    )
                )
            )
        except (
            StalePlanError,
            SourceMissingError,
            UnverifiedParseError,
            RestoredBehindError,
        ) as error:
            raise JobFailedError(_code_of(error)) from None
        except (NoLibraryError, KeyringUnavailableError):
            raise JobFailedError("job.failed") from None

    return run


def export_plan_destination(comp: Composition, plan_id: str) -> str:
    table = coherence.export_plan
    with comp.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(table.c.shelf_destination_id).where(table.c.plan_id == plan_id)
            ).scalar_one()
        )


class PlanService:
    """What the routes of the plans need (EF-EXP-008, EF-INT-001): a preview whose names Kavita
    has read, and the application of a plan queued as a job (the route answers 202 and the job)."""

    def __init__(
        self,
        comp: Composition,
        pipeline: Callable[[], ExportPipeline],
        pools: Callable[[], JobPools | None],
    ) -> None:
        self._comp = comp
        self._pipeline = pipeline
        self._pools = pools
        self._lock = threading.Lock()  # one preview at a time: the answers are bound to it

    def preview(self, destination_id: str) -> PlanPreview:
        """A Kavita that cannot be asked leaves the names unchecked: the plan can be read but the
        job will refuse to apply it (EF-EXP-045)."""
        pipeline = self._pipeline()
        with self._lock:
            try:
                destination = _destination_of(self._comp, destination_id)
                client = self._comp.kavita_client(destination.target_id)
                if _names_may_refresh(self._comp, destination_id):
                    asyncio.run(refresh_names(self._comp.engine, client, destination_id))
                asyncio.run(refresh_foreign_names(self._comp.engine, client, destination_id))
                names, kind = pipeline.wanted_names(destination_id)
                bulk = asyncio.run(BulkParse.fetch(client, names, library_type_named(kind)))
                pipeline.bind_parse(bulk, verified=True)
            except (KeyringUnavailableError, KeyError, sa.exc.NoResultFound, TargetError):
                pipeline.bind_parse(_no_remote_parse, verified=False)
            return pipeline.preview(destination_id)

    def queue_apply(self, plan_id: str, *, if_match: str) -> str:
        """The 412 and the pauses are answered now; what needs the disks and Kavita is the job's."""
        if self._comp.gates.writable is not None:
            self._comp.gates.writable()  # restored_behind: no application until the adoption
        ensure_applicable(self._comp.engine, plan_id, if_match=if_match)
        pools = self._pools()
        if pools is None:
            raise ExitUnavailableError("the job queue is not running")
        created = pools.submit(
            APPLY_JOB_KIND, dedupe_key=apply_key(plan_id, if_match), priority_class="request"
        )
        return created.job_id


# -- reconcile -----------------------------------------------------------------


def reconcile_handler(comp: Composition, *, in_window: bool) -> Callable[[JobContext], None]:
    """The verification of the tree, then the mirrors projected again if one is lost: both read
    the disks, so both are window jobs (ENF-OPS-014)."""

    def run(context: JobContext) -> None:
        destinations = tree_destinations(comp)
        if not destinations:
            return
        try:
            pipeline, reconciler, _fsops = build_pipeline(comp)
        except (NoLibraryError, KeyringUnavailableError):
            return
        for destination in destinations:
            if context.stop_requested:
                return
            if in_window:  # it looks at the tree anyway: what a pass learned of it is stale
                blocked_memo.forget(comp.engine, destination.id)
            reconciler.reconcile(destination.id, in_window=in_window)
            if in_window:
                pipeline.heal_mirror(destination.id)

    return run


# -- access audit -----------------------------------------------------------------


def connected_clients(comp: Composition) -> Callable[[], Iterable[tuple[str, KavitaClient]]]:
    """The client factory of the daily audit: every target that has an address and a sealed key."""

    def clients() -> Iterable[tuple[str, KavitaClient]]:
        for target_id in comp.kavita_targets():
            try:
                yield target_id, comp.kavita_client(target_id)
            except (KeyringUnavailableError, KeyError, sa.exc.NoResultFound):
                continue

    return clients


def access_audit(comp: Composition) -> Callable[[], None]:
    auditor = AccessAuditor(comp.engine, comp.clock, BusTargetEvents(comp.engine, comp.bus))
    return daily_access_audit(auditor, connected_clients(comp))


def key_expiry(comp: Composition) -> Callable[[], None]:
    """Read the key date of every connected target again and send the notices that are due."""
    clients = connected_clients(comp)

    def run() -> None:
        for target_id, client in clients():
            try:
                asyncio.run(client.check_key_expiry())
            except Exception as error:  # one unreachable target must not stop the others
                log.warning(
                    "target.key_expiry_failed", target_id=target_id, error=type(error).__name__
                )

    return run


def intents_prune(comp: Composition) -> Callable[[], int]:
    """Retention of the intent journal: closed intents resolved before the retention go."""

    def run() -> int:
        loaded = comp.load_roots()
        if loaded is None:  # a fresh install has no journal yet
            return 0
        journal = IntentJournal(comp.engine, comp.open_fsops(loaded), clock=comp.clock)
        return journal.prune(before=comp.clock.now() - INTENT_RETENTION)

    return run


# -- notifications -----------------------------------------------------------------


class LazyDispatcher:
    """The sender, built when the keyring exists (a fresh install has no channel to open)."""

    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def run_once(self) -> Any:
        try:
            vault = self._comp.vault()
        except KeyringUnavailableError:
            return None
        return NotificationDispatcher(
            self._comp.engine, vault, self._comp.clock, bus=self._comp.bus
        ).run_once()


# -- registration -----------------------------------------------------------------


def register_runtime_jobs(pools: JobPools, comp: Composition) -> None:
    pools.register(FOLLOW_JOB_KIND, pool="io", handler=follow_handler(comp, pools))
    pools.scheduler.add(FOLLOW_JOB_KIND, FOLLOW_CRON, jitter_s=120)
    pools.register(EXPORT_JOB_KIND, pool="io", handler=export_handler(comp))
    pools.register(APPLY_JOB_KIND, pool="io", handler=apply_handler(comp))
    pools.scheduler.add(EXPORT_JOB_KIND, EXPORT_CRON, jitter_s=60)
    pools.register(
        RECONCILE_KIND, pool="maint", handler=reconcile_handler(comp, in_window=False),
        windowed=True, writes=False,
    )  # fmt: skip
    pools.scheduler.add(RECONCILE_KIND, RECONCILE_CRON, jitter_s=60, windowed=True)
    pools.register(
        RECONCILE_HASH_KIND, pool="maint", handler=reconcile_handler(comp, in_window=True),
        windowed=True, writes=False,
    )  # fmt: skip
    pools.scheduler.add(RECONCILE_HASH_KIND, RECONCILE_HASH_CRON, windowed=True)
    key_check, prune = key_expiry(comp), intents_prune(comp)
    pools.register(KEY_EXPIRY_KIND, pool="io", handler=lambda _context: key_check(), writes=False)
    pools.scheduler.add(KEY_EXPIRY_KIND, KEY_EXPIRY_CRON, jitter_s=300)

    def run_prune(_context: JobContext) -> None:
        prune()

    pools.register(INTENT_PRUNE_KIND, pool="maint", handler=run_prune, writes=False)
    pools.scheduler.add(INTENT_PRUNE_KIND, INTENT_PRUNE_CRON, jitter_s=300)
    register_notification_job(pools, LazyDispatcher(comp))  # type: ignore[arg-type]
    register_store_jobs(pools, comp)  # the window jobs of /store (L14)
    register_safeguard_job(pools, comp)  # "Mettre à l'abri" and the optional purge (L16)
    register_retention_jobs(pools, comp)  # technical retention (ENF-OPS-009)
