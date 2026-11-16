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
- `target.library_type_check` and `target.major_update_replay` (daily, EF-EXP-049): the type of
  each Kavita library read again (a change pauses the shelf), and the names replayed through
  `parse-bulk` when the Kavita line changed; both only report, nothing is renamed or written;
- `intents.prune` (daily): the closed intents older than the retention leave the journal;
- `notify.dispatch` (EF-INT-006): the sender of the notification queue.

A job that finds nothing to do (no Suwayomi, no shelf, no keyring yet) returns quietly: a fresh
install runs every job and none of them touches anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePath
from typing import Any, cast

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine
from structlog.contextvars import bound_contextvars

from shelfhold.acquisition.public import (
    ACQUIRE_JOB_KIND,
    FOLLOW_JOB_KIND,
    CadencedFollowCycle,
    CadencePolicy,
    FollowCycle,
    FollowReport,
    LeasedReceiver,
    ReadStateRemote,
    RootGuard,
    bounded_receiver,
    epub_only,
    push_read_state,
)
from shelfhold.composition import (
    KAVITA_WAKE,
    SUWAYOMI_WAKE,
    Composition,
    KeyringUnavailableError,
    NoLibraryError,
    WriteContext,
)
from shelfhold.export.application import blocked_memo
from shelfhold.export.application.fan_out import (
    CachedInstanceProbe,
    EpubFile,
    FanOut,
    HostContentStore,
    TreeBarrier,
)
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.library_repair import LibraryRepair
from shelfhold.export.application.locks import work_lock_for
from shelfhold.export.application.migration_detect import SwitchoverDetector, SwitchoverGate
from shelfhold.export.application.naming_preview import (
    NamingPreview,
    contested_names,
    preview_names,
)
from shelfhold.export.application.pending_scans import remember_scans
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
from shelfhold.export.application.ranged_export import RangedExporter
from shelfhold.export.application.reconcile import Reconciler
from shelfhold.export.application.reconcile_full import FullReconciler
from shelfhold.export.application.rename_plan import (
    ItemMove,
    ReaderCount,
    ReaderCounter,
    RenameConfirmationRequiredError,
    RenamePreview,
    RenameRefusedError,
)
from shelfhold.export.application.rendering import cbz_renderer
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.application.upgrade import SamePathUpgrade
from shelfhold.export.application.waiting_marks import mark_mount_blocked
from shelfhold.export.application.wiring import NoRemoteParse
from shelfhold.export.domain.naming import DEFAULT_TEMPLATE, NamingTemplate
from shelfhold.export.domain.path_map import PathMapError
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.export.targets.kavita.accounts_audit import ChildAccountsAuditor
from shelfhold.export.targets.kavita.adopt import BulkParse, library_type_named
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.cover import SeriesCovers
from shelfhold.export.targets.kavita.destination import KavitaDestination
from shelfhold.export.targets.kavita.dto import LibraryType
from shelfhold.export.targets.kavita.errors import (
    RouteMissing,
    TargetError,
    TargetForbidden,
    TargetUnreachable,
)
from shelfhold.export.targets.kavita.library_type import LibraryTypeService
from shelfhold.export.targets.kavita.polling import ArrivalPoller
from shelfhold.export.targets.kavita.progress import KavitaProgress
from shelfhold.export.targets.kavita.reinstall import InstanceService
from shelfhold.export.targets.kavita.series import (
    SeriesAgeGate,
    SeriesAges,
    blocking_age_reader,
    blocking_unknowns_tier,
    refresh_foreign_names,
    refresh_names,
)
from shelfhold.export.targets.native_folder.source import NativeFolderSource
from shelfhold.identity.public import child_kavita_accounts
from shelfhold.import_jobs import register_import_job
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.observability.wiring import daily_access_audit
from shelfhold.infrastructure.packaging.cbz import PageTarget
from shelfhold.infrastructure.schema import coherence, export
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.wake import WakeDeferred
from shelfhold.integrations.apprise import NotificationDispatcher, register_notification_job
from shelfhold.jobs.daily_checks import library_type_check, major_update_replay
from shelfhold.jobs.digest_job import register_digest_job
from shelfhold.jobs.integrity_job import register_integrity_job
from shelfhold.jobs.public import JobContext, JobFailedError, JobPools
from shelfhold.jobs.thumbnail_gc import register_thumbnail_jobs
from shelfhold.library.public import (
    KeepCopier,
    PageFormatCbzStore,
    PassthroughStore,
    RangedKeeper,
    SvgCbzStore,
    UntrustedEpubStore,
)
from shelfhold.ops.acquire import AcquireOps
from shelfhold.ops.dedup import suggest_on_arrival
from shelfhold.ops.destination_accounts import open_destination_accounts
from shelfhold.ops.errors import ExitUnavailableError
from shelfhold.ops.events import BusTargetEvents
from shelfhold.ops.kavita_accounts import declared_children
from shelfhold.ops.library import LibraryOps
from shelfhold.ops.notifications import LazyNotificationSweep
from shelfhold.reading.application.sync import (
    DEFAULT_MAX_SESSION_HOURS,
    ProgressRemote,
    sync_destination,
)
from shelfhold.retention_jobs import register_retention_jobs
from shelfhold.safeguard_jobs import register_safeguard_job
from shelfhold.store_jobs import register_store_jobs
from shelfhold.work_locks import work_holder

__all__ = [
    "APPLY_JOB_KIND",
    "EXPORT_JOB_KIND",
    "INTENT_PRUNE_KIND",
    "KEY_EXPIRY_KIND",
    "LIBRARY_TYPE_CHECK_KIND",
    "MAJOR_UPDATE_REPLAY_KIND",
    "PROGRESS_SYNC_KIND",
    "READ_STATE_PUSH_KIND",
    "RECONCILE_HASH_KIND",
    "RECONCILE_KIND",
    "PlanService",
    "progress_sync_handler",
    "read_state_push_handler",
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
PROGRESS_SYNC_KIND = "progress.sync"  # EF-EXP-026: the members' progress, with Kavita
PROGRESS_SYNC_CRON = "*/10 * * * *"
READ_STATE_PUSH_KIND = "read_state.push"  # EF-SRC-026: the owner's « read », pushed to Suwayomi
READ_STATE_PUSH_CRON = "*/10 * * * *"
RECONCILE_CRON = "*/10 * * * *"  # window job: runs every 10 minutes of the window only
INTENT_PRUNE_KIND = "intents.prune"
KEY_EXPIRY_KIND = "target.key_expiry"
LIBRARY_TYPE_CHECK_KIND = "target.library_type_check"
MAJOR_UPDATE_REPLAY_KIND = "target.major_update_replay"
INTENT_PRUNE_CRON = "41 3 * * *"
KEY_EXPIRY_CRON = "23 6 * * *"
LIBRARY_TYPE_CHECK_CRON = "47 6 * * *"
MAJOR_UPDATE_REPLAY_CRON = "52 6 * * *"
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


class _LazyUpgrader:
    """The same-path upgrade of a better-ranked copy (EF-SRC-037), on the write context the cycle
    opens when a chapter is received: a cycle that upgrades nothing takes no lease for it."""

    def __init__(self, comp: Composition, lazy: _LazyWrite) -> None:
        self._comp = comp
        self._lazy = lazy

    def upgrade_unit(self, unit_id: str, content_id: str) -> list[str]:
        w = self._lazy.get()
        upgrade = SamePathUpgrade(
            self._comp.engine, w.fsops, locate=w.loaded.locate,
            data_dir=self._comp.settings.data_dir, clock=self._comp.clock, bus=w.bus,
            renderer=cbz_renderer(
                w.fsops, staging_dir=self._comp.settings.data_dir / "tmp",
                target=PageTarget(
                    self._comp.settings.page_target_format, self._comp.settings.page_target_quality
                ),
                capabilities=FirstPartyDestinations(self._comp.engine).capabilities,
            ),
        )  # fmt: skip
        return [r.outcome.value for r in upgrade.upgrade_unit(unit_id, content_id)]


class _LazyRepairer:
    """Fresh bytes of a corrupt Unit put in the place of the damaged file (EF-BIB-012), on the
    write context the cycle opens when a chapter is received."""

    def __init__(self, comp: Composition, lazy: _LazyWrite) -> None:
        self._comp = comp
        self._lazy = lazy

    def __call__(self, unit_id: str, variant_id: str, staged: Path, sha256: str) -> bool:
        w = self._lazy.get()
        repair = LibraryRepair(
            self._comp.engine, w.fsops, w.loaded.locate, self._comp.clock,
            hold_work=work_holder(self._comp), bus=w.bus,
            data_dir=self._comp.settings.data_dir,
        )  # fmt: skip
        return repair(unit_id, variant_id, staged, sha256)


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


@contextmanager
def _no_hold(_work_id: str) -> Iterator[None]:
    yield


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
            tombstones=w.tombstones, hold_work=_no_hold,
            min_page_ratio=self._comp.settings.revision_min_page_ratio,
        )  # fmt: skip
        keeper = KeepCopier(
            self._comp.engine, w.fsops, w.loaded.locate, str(self._comp.settings.store_dir),
            self._comp.settings.data_dir,
            IntentJournal(self._comp.engine, w.fsops, clock=self._comp.clock),
            hold_work=_no_hold,
        )  # fmt: skip
        # EF-BIB-051 (6): an untrusted CBZ with SVG pages is drawn to PNG and declared a derivative,
        # the original and the derivative filed under one hold of the Work
        hold = work_holder(self._comp)
        svg = SvgCbzStore(store, keeper, self._comp.settings.data_dir, self._comp.engine, hold)
        # EF-EXP-046 (ADR-0054): pages in other formats are normalized, the original kept in /store
        store_dir = str(self._comp.settings.store_dir)
        return PageFormatCbzStore(
            svg, store, keeper, self._comp.settings.data_dir, self._comp.engine, hold,
            target=PageTarget(
                self._comp.settings.page_target_format, self._comp.settings.page_target_quality
            ),
            keeps_originals=any(e.path == store_dir for e in w.loaded.expected),
        ).ingest(**arguments)  # fmt: skip


def acquire_handler(comp: Composition, pools: JobPools) -> Callable[[JobContext], None]:
    """One request of a person (EF-SRC-015): the file of one `download_request` of a native
    source, fetched under the source's own Fetcher and filed like any received file. The lease on
    the library is taken first: nothing is downloaded that cannot be filed."""

    def run(context: JobContext) -> None:
        request_id = context.dedupe_key.removeprefix(f"{ACQUIRE_JOB_KIND}:")
        try:
            acquirer = AcquireOps(comp).acquirer()
            with ExitStack() as stack:
                lazy = _LazyWrite(comp, stack)
                lazy.get()
                outcome = asyncio.run(
                    acquirer.fetch(
                        request_id, cast("Any", _LazyStore(comp, lazy)),
                        lambda variant, extension: lazy.get().received_path(variant, extension),
                    )
                )  # fmt: skip
        except (NoLibraryError, KeyringUnavailableError, ExitUnavailableError) as error:
            log.info("acquire.skipped", reason=type(error).__name__)
            context.record(skipped=type(error).__name__)
            return
        context.record(state=outcome.state)
        if outcome.state == "fetched":  # what arrived goes out
            pools.submit(EXPORT_JOB_KIND, dedupe_key=EXPORT_JOB_KIND)

    return run


def follow_handler(comp: Composition, pools: JobPools) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        arrived: list[int] = []
        links: list[Any] = []
        for build in (comp.suwayomi, comp.lncrawl):
            try:
                link = build()
            except KeyringUnavailableError as error:
                # a locked keyring stops the service that needs a key, never the others
                log.info("follow.link_skipped", reason=type(error).__name__)
                continue
            if link is not None:
                links.append(link)
        if not links:
            log.debug("follow.skipped", reason="no_connected_service")
            context.record(skipped="no_connected_service")
            return  # no connected service configured yet
        closed: set[int] = set()
        try:
            with ExitStack() as stack:
                lazy = _LazyWrite(comp, stack)

                def place(_unit: str, variant_id: str, filename: str) -> str:
                    return lazy.get().received_path(variant_id, PurePath(filename).suffix)

                keeper = RangedKeeper(comp.settings.data_dir)
                keeper.sweep(comp.engine)  # files of a transaction that did not commit
                for position, link in enumerate(links):
                    cycle = CadencedFollowCycle(
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
                        keep_range=epub_only(keeper),  # EF-SRC-034: an EPUB container, nothing else
                        tombstones=cast("Any", _LazyTombstones(comp)),
                        wakes=comp.wakes(),
                        check_mode="piloted" if comp.check_mode() == "piloted" else "always_on",
                        min_page_ratio=comp.settings.revision_min_page_ratio,
                        fallback_return=comp.settings.follow_fallback_return,
                        upgrader=_LazyUpgrader(comp, lazy),
                        repairer=_LazyRepairer(comp, lazy),
                        policy=CadencePolicy.from_settings(comp.settings),
                        root_guard=RootGuard(),  # EF-EXP-010: the loop guard, every cycle
                    )  # fmt: skip
                    prefix = "" if position == 0 else f"{link.service.manifest.key}_"

                    async def once(cycle: FollowCycle = cycle, link: Any = link) -> FollowReport:
                        try:
                            return await cycle.run()
                        finally:
                            closed.add(id(link))
                            await link.guarded.aclose()

                    report = asyncio.run(once())
                    context.record(**{f"{prefix}{k}": v for k, v in report.counters().items()})
                    arrived.append(report.fetched)
                    if report.counters().get("created"):  # EF-BIB-004: a title may repeat
                        suggest_on_arrival(comp)
        except (NoLibraryError, KeyringUnavailableError) as error:
            log.info("follow.cycle_skipped", reason=type(error).__name__)
            context.record(skipped=type(error).__name__)
            return
        finally:
            for link in links:  # a service the cycle never reached has its fetcher closed too
                if id(link) not in closed:
                    with contextlib.suppress(Exception):
                        asyncio.run(link.guarded.aclose())
        if any(arrived):  # what arrived goes out; a quiet cycle asks for nothing (the cron does)
            pools.submit(EXPORT_JOB_KIND, dedupe_key=EXPORT_JOB_KIND)

    return run


# -- export.run -----------------------------------------------------------------


class ScanRelay:
    """`request_scan` of the pipeline, forwarded to the client of the destination being exported."""

    def __init__(self, engine: Engine | None = None) -> None:
        self.client: KavitaClient | None = None
        self.asked: list[str] = []
        self._engine = engine  # where a scan that could not be sent is written down

    def lost(self, paths: list[str]) -> None:
        if self._engine is not None and paths:
            remember_scans(self._engine, paths)

    def __call__(self, remote_path: str) -> None:
        if self.client is not None:
            self.client.request_scan(remote_path)
            self.asked.append(remote_path)

    def drain(self, allowed: Callable[[str], bool] | None = None) -> None:
        """Send the scans asked so far, now: the end of a batch of a rename plan (EF-EXP-030), so
        that Kavita sees each batch when it is done and not when the whole plan is. The plan code
        is synchronous and runs inside the loop of the job: the call has its own loop, in its own
        thread. Kavita lost: the scans are written down, and asked again by the next run."""
        client = self.client
        if client is None or not self.asked:
            return
        from concurrent.futures import ThreadPoolExecutor

        paths = list(self.asked)
        self.asked.clear()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(lambda: asyncio.run(client.flush_scans(allowed, now=True))).result()
        except TargetError as error:
            log.info("export.scan_failed", reason=type(error).__name__)
            self.lost(paths)


def build_pipeline(
    comp: Composition, scans: ScanRelay | None = None
) -> tuple[ExportPipeline, Reconciler, Any]:
    fsops, loaded, _tombstones = comp.read_context()
    reconciler = Reconciler(comp.engine, fsops, clock=comp.clock, bus=comp.bus)
    renderer = cbz_renderer(
        fsops,
        staging_dir=comp.settings.data_dir / "tmp",
        target=PageTarget(comp.settings.page_target_format, comp.settings.page_target_quality),
        capabilities=FirstPartyDestinations(comp.engine).capabilities,
    )  # EF-EXP-046: what each destination takes, by its `accepted_image_types`
    pipeline = ExportPipeline(
        comp.engine, fsops, locate=loaded.locate, data_dir=comp.settings.data_dir,
        clock=comp.clock, parse=_unchecked, parse_verified=False, renderer=renderer,
        segment_policy_confirmed=comp.settings.naming_segment_policy_confirmed,
        sessions=comp.session_opener(fsops, loaded), request_scan=scans,
        detector=lambda destination_id: reconciler.reconcile(destination_id),  # EF-EXP-043
        gate=comp.gates.writable, operating_bus=comp.bus,
    )  # fmt: skip
    pipeline.use_preflight(PermissionPreflight(comp.engine, fsops, comp.bus, clock=comp.clock))
    pipeline.use_switchover(
        SwitchoverGate(comp.engine, SwitchoverDetector(comp.engine, fsops), clock=comp.clock)
    )
    # the executor of the plans destinations describe (M4 L04), with the bytes the host owns and
    # the identity each server gives now (asked at most every 30 s, whatever the plan's length)
    pipeline.use_plan_executor(
        contents=HostContentStore(
            comp.engine,
            capabilities=FirstPartyDestinations(comp.engine).capabilities,
            renders=FirstPartyDestinations(comp.engine).renders,  # EF-EXP-010: a ComicInfo each
            epub_path=_epub_path_of(comp, fsops),
            fsops=fsops,
        ),
        probe=CachedInstanceProbe(_instance_identity(comp), clock=comp.clock),
        events=BusTargetEvents(comp.engine, comp.bus),
        barrier=TreeBarrier(comp.engine),  # the barrier of a family shelf, read again as it runs
    )
    return pipeline, reconciler, fsops


def _epub_path_of(comp: Composition, fsops: Fsops) -> Callable[[str], EpubFile | None]:
    """The one file of a Unit's EPUB the host may serve: its verified derivative, never an
    original (EF-BIB-051). A Unit with none gives nothing."""
    from shelfhold.library.public import OriginalNotServableError, servable_epub

    def path(unit_id: str) -> EpubFile | None:
        staging = comp.settings.data_dir / "tmp"
        try:
            served = servable_epub(
                comp.engine, fsops, unit_id, staging=staging if staging.is_dir() else None
            )
            return EpubFile(served.path, served.file_hash, served.content_id)
        except OriginalNotServableError:
            return None

    return path


def _instance_identity(comp: Composition) -> Callable[[str], str | None]:
    """What the server of a target says of itself, read on a thread of its own (the plan code is
    synchronous and may run inside the loop of a job). A server that does not answer, or a target
    with no sealed key, gives nothing: one that was known then pauses its binding."""

    def fetch(target_id: str) -> str | None:
        from concurrent.futures import ThreadPoolExecutor

        async def ask(client: KavitaClient) -> str | None:
            await client.probe()
            return await InstanceService.fingerprint(client)

        try:
            client = comp.kavita_client(target_id)
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(ask(client))).result()
        except (KeyringUnavailableError, KeyError, sa.exc.NoResultFound, TargetError):
            return None

    return fetch


class FirstPartyDestinations:
    """What the destinations of the core describe for a binding. Kavita is run by its own client
    (`KavitaClient.execute`, with the key the core holds): it describes calls the core already
    sends and no plan of files, so nothing is asked of it here. The native folder (EF-EXP-010)
    describes a plan of files: `NativeFolderSource` reads what was planned and answers for it."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._native = NativeFolderSource(engine, root_check=RootGuard().check_root)

    def attach(self, host: Any) -> None:
        self._native.attach(host)

    def capabilities(self, target_id: str) -> Any:
        with self._engine.connect() as connection:
            kind = connection.execute(
                sa.select(export.export_target.c.kind).where(export.export_target.c.id == target_id)
            ).scalar()
        if kind == "native_folder":
            return self._native.capabilities(target_id)
        return KavitaDestination.capabilities if kind == "kavita" else None

    def plan(self, binding: Any) -> object | None:
        return self._native.plan(binding)  # None for every other kind

    def sidecars(self, binding: Any) -> frozenset[str]:
        return self._native.sidecars(binding)

    def renders(self, target_id: str) -> bool:
        return self._native.renders(target_id)

    def calls(self, _binding: Any) -> tuple[object, ...]:
        return ()

    def sender(self, _binding: Any) -> Callable[[Any], object]:
        def refuse(_call: Any) -> object:
            raise TargetError("no destination of the core sends calls through the executor yet")

        return refuse


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

    @property
    def opened(self) -> bool:
        return self._built is not None


def export_handler(comp: Composition) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        destinations = tree_destinations(comp)
        context.record(destinations=len(destinations))
        if not destinations:
            return
        try:  # scans a failure left (deleted series, withdrawals, detachments): asked again here
            context.add(scans_retried=sum(LibraryOps(comp).retry_pending_scans().values()))
        except Exception as error:  # never stops the export itself
            log.info("export.scan_retry_failed", reason=type(error).__name__)
        scans = ScanRelay(comp.engine)
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
        natives = _native_folders(comp, destinations)
        if disks.opened or natives:  # each shelf's bindings are judged one by one (L06a, L07)
            _fan_out_pass(comp, disks, destinations if disks.opened else natives, context)

    return run


def _native_folders(comp: Composition, destinations: list[Destination]) -> list[Destination]:
    """The bindings of the run that are a native folder: no server to ask, only a folder to write,
    so they take part in the pass even when no Kavita opened a root (EF-EXP-010)."""
    if not destinations:
        return []
    table = export.export_target
    with comp.engine.connect() as connection:
        native = set(
            connection.execute(
                sa.select(table.c.id).where(
                    table.c.id.in_({d.target_id for d in destinations}),
                    table.c.kind == "native_folder",
                )
            ).scalars()
        )
    return [d for d in destinations if d.target_id in native]


def _fan_out_pass(
    comp: Composition, disks: _Disks, destinations: list[Destination], context: JobContext
) -> None:
    """The pass over the bindings of the shelves this run exported: the barrier of a shelf below
    18+ looked at again for each binding, what each is given, and the calls and plans the
    destinations describe through the executor (the gates of the pipeline's doors). Nothing here
    opens a root: the pipeline of the run is the one that was already built."""
    from shelfhold.export.application.target_state import DestinationPausedError

    pipeline, _fsops = disks.get()
    destinations_of_core = FirstPartyDestinations(comp.engine)
    destinations_of_core.attach(pipeline.plan_contents)  # a cover is named by its bytes
    fan = FanOut(comp.engine, pipeline, destinations_of_core, clock=comp.clock)
    with comp.engine.connect() as connection:
        shelves = sorted(
            set(
                connection.execute(
                    sa.select(export.shelf_destination.c.shelf_id).where(
                        export.shelf_destination.c.id.in_([d.id for d in destinations])
                    )
                ).scalars()
            )
        )
    for shelf_id in shelves:
        try:
            fan.run(shelf_id, new_ulid())
        except (DestinationPausedError, RestoredBehindError) as error:
            log.info("export.fan_out_waits", reason=type(error).__name__)
        except Exception as error:  # one shelf never stops the job nor the shelves after it
            log.warning("export.fan_out_failed", reason=type(error).__name__)
            context.add(fan_out_failed=1)  # a counter of the run: Health reads it


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
    with write_transaction(comp.engine) as connection:
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
            # a kept volume is read under /data only, never a library
            ranged_hash=RangedExporter(comp.engine, comp.settings.data_dir).source_hash,
        )  # fmt: skip
        if names or apply is not None:
            events = BusTargetEvents(comp.engine, comp.bus)
            # the type of the libraries, read at each connection that is about to write: a type
            # that changed pauses the shelf, and the pipeline below refuses to run (EF-EXP-049)
            await LibraryTypeService(comp.engine, comp.clock, events).check(
                client, destination.target_id
            )
            pipeline, _fsops = disks.get()
            if apply is not None and pipeline.kind_of(apply[0]) == "rename_plan":
                pipeline.bind_readers(reader_counter(comp, destination))  # EF-EXP-037
                guard = _scan_guard(comp, disks, destination.id)
                pipeline.bind_scan_drain(lambda: scans.drain(guard))  # one scan per batch
                names = []  # a rename plan names nothing: the files exist, only their place moves
            bulk = await BulkParse.fetch(client, names, library_type_named(kind))
            pipeline.bind_parse(bulk, verified=True)
            # EPUB series (EF-EXP-040): the ages due pushed, then read back locked, before any
            # plan; Kavita failing keeps the gate closed and no EPUB beyond what it shows leaves
            gate = SeriesAgeGate(comp.engine, comp.clock, events)
            pipeline.bind_age_gate(
                await gate.open(client, destination.id),
                # the last look before an EPUB write reads Kavita again, under the locks
                blocking_age_reader(lambda: comp.kavita_client(destination.target_id)),
                blocking_unknowns_tier(
                    comp.engine, lambda: comp.kavita_client(destination.target_id), destination.id
                ),
            )
            if apply is None:
                report = pipeline.run(destination.id, job_id=context.job_id)
            else:
                plan_id, version = apply
                if pipeline.kind_of(plan_id) != "rename_plan":
                    pipeline.require_names_checked(plan_id)
                # the confirmation of a rename plan is the one its request recorded (a session's)
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
            except TargetError as error:  # Kavita lost after the writing: written down, asked again
                log.info("export.scan_failed", destination_id=destination.id,
                         reason=type(error).__name__)  # fmt: skip
                scans.lost(list(scans.asked))
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


def apply_key(plan_id: str, if_match: str, *, confirmed: bool = False) -> str:
    """The dedupe key of the job: the plan and the validator of its preview. Two requests for the
    same plan share one job while it is active; a plan applied once is `done` and refused. The
    confirmation of a rename plan is not part of it (`confirmed` is ignored): it is recorded with
    the plan by the session that gave it and checked when the plan is applied (EF-EXP-037), so a
    key that replays a job cannot bring a confirmation of its own."""
    return f"{APPLY_JOB_KIND}:{plan_id}:{if_match}"


def _plan_of(dedupe_key: str) -> tuple[str, str]:
    _kind, plan_id, rest = dedupe_key.split(":", 2)
    return plan_id, rest.partition(":")[0]


def reader_counter(comp: Composition, destination: Destination) -> ReaderCounter:
    """The readers of Kavita in some folders of a destination (EF-EXP-037), asked now. The call
    runs its own event loop in its own thread: the plan code is synchronous, and the job that
    applies it already runs inside a loop."""
    from concurrent.futures import ThreadPoolExecutor

    from shelfhold.export.targets.kavita.readers import count_readers

    def count(folders: Sequence[str]) -> ReaderCount:
        def ask(library: int) -> ReaderCount:
            # the client is made here, in the thread with no running loop: building it runs
            # a loop of its own (the fetcher of the integrations)
            client = comp.kavita_client(destination.target_id)
            return asyncio.run(count_readers(client, library, list(folders)))

        try:
            library = _remote_library_id(comp, destination.id)
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(ask, library).result()
        except (KeyringUnavailableError, KeyError, sa.exc.NoResultFound, ValueError):
            return ReaderCount(0, exact=False)

    return count


def _remote_library_id(comp: Composition, destination_id: str) -> int:
    link, library = export.shelf_destination_remote_library, export.remote_library
    with comp.engine.connect() as connection:
        ref = connection.execute(
            sa.select(library.c.remote_ref)
            .join(link, link.c.remote_library_id == library.c.id)
            .where(link.c.shelf_destination_id == destination_id)
            .limit(1)
        ).scalar_one()
    return int(ref)


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
        scans = ScanRelay(comp.engine)
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
                        comp,
                        disks,
                        client,
                        destination,
                        scans,
                        context,
                        (plan_id, version),
                    )
                )
            )
        except (
            StalePlanError,
            SourceMissingError,
            UnverifiedParseError,
            RestoredBehindError,
            RenameConfirmationRequiredError,
            RenameRefusedError,
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


async def _read_names(
    client: KavitaClient, names: list[str], library_type: LibraryType
) -> BulkParse:
    """The answers of `plugin/parse` for the names: in bulk, or one by one when the bulk route is
    gone from this Kavita (the single-name route is then the way to check a name)."""
    try:
        return await BulkParse.fetch(client, names, library_type)
    except RouteMissing:
        return BulkParse({name: await client.parse_name(name, library_type) for name in set(names)})


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

    def flagged_names(self, destination_id: str) -> int:
        """The names the destination's own template puts in question (two files on one path, a
        name cut or reserved, a Unit the planner refuses for its name): from the database
        alone, no Kavita asked and no lock taken (the one of `preview` is held while Kavita
        answers), so the list of the shelves never waits on a sleeping server."""
        items, existing, _kind, refused = self._pipeline().naming_state(destination_id)
        with self._comp.engine.connect() as conn:
            stored = conn.execute(
                sa.select(export.shelf_destination.c.template).where(
                    export.shelf_destination.c.id == destination_id
                )
            ).scalar_one()
        preview = preview_names(
            items, NamingTemplate.loads(stored), parse=None, verified=False, existing=existing
        )
        return contested_names(preview) + refused

    def naming_preview(
        self, destination_id: str, template: NamingTemplate | None = None
    ) -> NamingPreview:
        """What `template` would call each Unit the shelf is yet to write, each name read by
        Kavita's `plugin/parse` (EF-EXP-005). Read-only: nothing is written, no disk is looked at.
        A Kavita that cannot be asked, or that the parser replica does not cover, leaves the
        names "unverified"; it never blocks."""
        pipeline = self._pipeline()
        candidate = template or DEFAULT_TEMPLATE
        with self._lock:
            items, existing, kind = pipeline.naming_inputs(destination_id)
            asked: list[str] = []

            def record(name: str) -> Any:
                asked.append(name)
                return _no_remote_parse(name)

            preview_names(items, candidate, parse=record, verified=True, existing=existing)
            try:
                destination = _destination_of(self._comp, destination_id)
                client = self._comp.kavita_client(destination.target_id)
                asyncio.run(client.probe())  # the version the replica must cover is read now
                bulk = asyncio.run(_read_names(client, asked, library_type_named(kind)))
                verified = client.preview_verified
                return preview_names(
                    items, candidate, parse=bulk, verified=verified, existing=existing
                )
            except (KeyringUnavailableError, KeyError, sa.exc.NoResultFound, TargetError):
                return preview_names(
                    items, candidate, parse=None, verified=False, existing=existing
                )

    def kind_of(self, plan_id: str) -> str:
        return self._pipeline().kind_of(plan_id)

    def rename_preview(
        self,
        destination_id: str,
        *,
        template: NamingTemplate | None = None,
        moves: Sequence[ItemMove] = (),
    ) -> RenamePreview:
        """The renames a template change, a reshelving or a binding migration makes, with the
        readers of Kavita who would lose their progress (EF-EXP-030, EF-EXP-037)."""
        pipeline = self._pipeline()
        with self._lock:
            pipeline.bind_readers(
                reader_counter(self._comp, _destination_of(self._comp, destination_id))
            )
            return pipeline.rename_preview(destination_id, template=template, moves=moves)

    def queue_apply(
        self, plan_id: str, *, if_match: str, confirm_progress_loss: bool = False
    ) -> str:
        """The 412, the pauses and the confirmation of a rename plan are answered now; what
        needs the disks and Kavita is the job's."""
        if self._comp.gates.writable is not None:
            self._comp.gates.writable()  # restored_behind: no application until the adoption
        destination_id = ensure_applicable(self._comp.engine, plan_id, if_match=if_match)
        pipeline = self._pipeline()
        if pipeline.kind_of(plan_id) == "rename_plan":
            pipeline.require_not_applying(plan_id)  # 409: a job is on it, the plan is left be
            with self._lock:
                pipeline.bind_readers(
                    reader_counter(self._comp, _destination_of(self._comp, destination_id))
                )
                pipeline.require_confirmation(plan_id, confirmed=confirm_progress_loss)
        pools = self._pools()
        if pools is None:
            raise ExitUnavailableError("the job queue is not running")
        created = pools.submit(
            APPLY_JOB_KIND,
            dedupe_key=apply_key(plan_id, if_match),
            priority_class="request",
        )
        return created.job_id


# -- reconcile -----------------------------------------------------------------


def reconcile_handler(comp: Composition, *, in_window: bool) -> Callable[[JobContext], None]:
    """The verification of the tree (in the hash window: the full inventory of drift, orphans,
    duplicates and links, EF-EXP-018), then the mirrors projected again if one is lost: all read
    the disks, so all are window jobs (ENF-OPS-014)."""

    def run(context: JobContext) -> None:
        destinations = tree_destinations(comp)
        if not destinations:
            return
        try:
            pipeline, reconciler, fsops = build_pipeline(comp)
        except (NoLibraryError, KeyringUnavailableError):
            return
        inventory = FullReconciler(comp.engine, fsops, clock=comp.clock, bus=comp.bus)

        def out_of_time() -> bool:  # the window is over, or SIGTERM (ENF-OPS-014)
            over = context.window_end is not None and context.clock.now() >= context.window_end
            return over or context.stop_requested

        for destination in destinations:
            if out_of_time():
                return
            if in_window:  # it looks at the tree anyway: what a pass learned of it is stale
                blocked_memo.forget(comp.engine, destination.id)
            reconciler.reconcile(destination.id, in_window=in_window)
            if in_window:  # the full pass reads the hashes: only where files may be hashed
                inventory.scan(destination.id, in_window=True, should_stop=out_of_time)
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
    """The daily audit: the libraries' side (pauses a shelf) and the children's side (tells the
    admin, once, when the Kavita account of a child reaches too much, M3 L10). Both read only."""
    events = BusTargetEvents(comp.engine, comp.bus)
    libraries_side = daily_access_audit(
        AccessAuditor(comp.engine, comp.clock, events), connected_clients(comp)
    )
    accounts = ChildAccountsAuditor(comp.engine, comp.clock, events)

    def run() -> None:
        try:
            libraries_side()
        except Exception as error:  # the libraries' side must not hide the children's side
            log.warning("target.access_audit_failed", error=type(error).__name__)
        children = declared_children(child_kavita_accounts(comp.engine))
        for target_id in comp.kavita_targets():
            try:
                client = comp.kavita_client(target_id)
            except Exception as error:  # a key that cannot be unsealed: unverified, never "ok"
                accounts.record_failure(target_id, error)
                continue
            asyncio.run(accounts.audit(client, target_id, children))

    return run


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


def progress_sync_handler(
    engine: sa.Engine,
    remote_of: Callable[[], ProgressRemote],
    clock: Any,
    page_map_keyring: Callable[[], Keyring] | None = None,
    max_session_hours: float = DEFAULT_MAX_SESSION_HOURS,
) -> Callable[[JobContext], None]:
    """`progress.sync` (EF-EXP-026): for every destination with the synchronisation on, each linked
    member in turn, pull then push. A Kavita that cannot be asked, a key Kavita refuses, a revoked
    one: the member's pass ends with a closed code and is counted (`paused`, `stopped`), never
    raised. Only a locked keyring leaves the job idle."""

    def run(context: JobContext) -> None:
        try:
            remote = remote_of()
            destinations = progress_destinations(engine)
            if not destinations:
                context.record(skipped="no_destination")
                return
            for destination in destinations:
                if context.stop_requested:
                    break
                for outcome in sync_destination(
                    engine,
                    remote,
                    destination_id=destination,
                    clock=clock,
                    should_stop=lambda: bool(context.stop_requested),
                    page_map_keyring=page_map_keyring,
                    max_session_hours=max_session_hours,
                ).values():
                    if outcome.code == "ok":
                        context.add(synchronised=1, pulled=outcome.pulled, pushed=outcome.pushed)
                    elif outcome.code == "unavailable":
                        context.add(paused=1)  # Kavita cannot be asked: the next run goes on
                    else:
                        context.add(stopped=1)  # a key to look at: the code is in the log
        except KeyringUnavailableError as error:
            log.info("progress_sync.skipped", reason=type(error).__name__)
            context.record(skipped="keyring_unavailable")

    return run


def read_state_push_handler(
    engine: sa.Engine,
    remote_of: Callable[[], ReadStateRemote | None],
    clock: Any,
    awake: Callable[[], bool] = lambda: True,
) -> Callable[[JobContext], None]:
    """`read_state.push` (EF-SRC-026): the owner profile's « read » to Suwayomi, in batches. A
    Suwayomi that sleeps or cannot be asked is a pause (`paused`), a refusal or a stop is counted
    (`stopped`); nothing raises, and nothing is lost: the notes follow what was written."""

    def run(context: JobContext) -> None:
        try:
            remote = remote_of()
        except KeyringUnavailableError as error:
            log.info("read_state_push.skipped", reason=type(error).__name__)
            context.record(skipped="keyring_unavailable")
            return
        if remote is None:
            context.record(skipped="no_suwayomi")
            return
        report = asyncio.run(
            push_read_state(
                engine, remote, clock=clock, awake=awake,
                should_stop=lambda: bool(context.stop_requested),
            )
        )  # fmt: skip
        context.add(pushed_read=report.pushed_read, pushed_unread=report.pushed_unread)
        if report.code in ("asleep", "unavailable"):
            context.add(paused=1)
        elif report.code != "ok":
            context.add(stopped=1)  # an owner to look at, a refusal, a stop: the code is in the log
            log.info("read_state_push.ended", code=report.code)

    return run


def progress_destinations(engine: sa.Engine) -> list[str]:
    table, target = export.shelf_destination, export.export_target
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(table.c.id)
            .join(target, target.c.id == table.c.target_id)
            .where(
                table.c.sync_progress.is_(True),
                table.c.enabled.is_(True),
                target.c.kind == "kavita",
            )
            .order_by(table.c.id)
        )
        return [str(r.id) for r in rows]


def progress_remote(comp: Composition) -> ProgressRemote:
    accounts = open_destination_accounts(
        comp.engine, comp.vault, clock=comp.clock, fetcher_for=comp.network.one_shot
    )
    return KavitaProgress(
        comp.engine,
        admin_client=comp.kavita_client,
        fetcher_for=comp.network.one_shot,
        open_token=accounts.open_token,
    )


def register_runtime_jobs(pools: JobPools, comp: Composition) -> None:
    pools.register(FOLLOW_JOB_KIND, pool="io", handler=follow_handler(comp, pools))
    pools.scheduler.add(FOLLOW_JOB_KIND, FOLLOW_CRON, jitter_s=120)
    pools.register(ACQUIRE_JOB_KIND, pool="io", handler=acquire_handler(comp, pools))
    pools.register(EXPORT_JOB_KIND, pool="io", handler=export_handler(comp))
    pools.register(APPLY_JOB_KIND, pool="io", handler=apply_handler(comp))
    pools.scheduler.add(EXPORT_JOB_KIND, EXPORT_CRON, jitter_s=60)
    pools.register(
        PROGRESS_SYNC_KIND, pool="io",
        handler=progress_sync_handler(
            comp.engine, lambda: progress_remote(comp), comp.clock, comp.keyring,
            comp.settings.reading_relay_max_session_hours,
        ),
    )  # fmt: skip
    pools.scheduler.add(PROGRESS_SYNC_KIND, PROGRESS_SYNC_CRON, jitter_s=60)
    pools.register(
        READ_STATE_PUSH_KIND, pool="io",
        handler=read_state_push_handler(
            comp.engine, comp.suwayomi_read_state, comp.clock,
            lambda: comp.wakes().is_awake(SUWAYOMI_WAKE),
        ),
    )  # fmt: skip
    pools.scheduler.add(READ_STATE_PUSH_KIND, READ_STATE_PUSH_CRON, jitter_s=60)
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

    events = BusTargetEvents(comp.engine, comp.bus)
    type_check = library_type_check(comp.engine, comp.clock, events, connected_clients(comp))
    replay = major_update_replay(comp.engine, comp.clock, events, connected_clients(comp))
    pools.register(
        LIBRARY_TYPE_CHECK_KIND, pool="io", handler=lambda _context: type_check(), writes=False
    )
    pools.scheduler.add(LIBRARY_TYPE_CHECK_KIND, LIBRARY_TYPE_CHECK_CRON, jitter_s=300)
    pools.register(
        MAJOR_UPDATE_REPLAY_KIND, pool="io", handler=lambda _context: replay(), writes=False
    )
    pools.scheduler.add(MAJOR_UPDATE_REPLAY_KIND, MAJOR_UPDATE_REPLAY_CRON, jitter_s=300)

    def run_prune(_context: JobContext) -> None:
        prune()

    pools.register(INTENT_PRUNE_KIND, pool="maint", handler=run_prune, writes=False)
    pools.scheduler.add(INTENT_PRUNE_KIND, INTENT_PRUNE_CRON, jitter_s=300)
    register_notification_job(pools, LazyDispatcher(comp))  # type: ignore[arg-type]
    register_digest_job(pools, LazyNotificationSweep(comp))  # EF-INT-021: per-person notices
    register_store_jobs(pools, comp)  # the window jobs of /store (L14)
    register_safeguard_job(pools, comp)  # "Mettre à l'abri" and the optional purge (L16)
    register_integrity_job(pools, comp)  # the rolling integrity check (M3 L23)
    register_thumbnail_jobs(pools, comp)  # cover thumbnails: collect, catch up (M3 L24)

    def export_stored() -> None:
        pools.submit(EXPORT_JOB_KIND, dedupe_key=EXPORT_JOB_KIND)

    register_import_job(
        pools, comp, export_stored
    )  # the import folder (EF-SRC-012), only when `import_dir` is set
    register_retention_jobs(pools, comp)  # technical retention (ENF-OPS-009)
