"""The periodic jobs of the runtime, registered before `boot_jobs`' scan (PLAN-M1a section 7).

- `follow.cycle` (L18): the always-on cycle of the connected service, its archives received into
  the inbox of the first writable root, under the lease, with the tombstone gate;
- `export.run` (L27): per destination, the foreign-writer detector, the plan checked by
  `plugin/parse`, the writing, the targeted scans and the arrival poll (EF-EXP-034);
- `reconcile.cycle` and `reconcile.hash` (L32): the reduced reconcile of a tree destination, the
  second one in the maintenance window where files may be hashed (ENF-OPS-014);
- `target.access_audit` (L26, L37) and `ops.disk_check` (L37): see `infrastructure.observability`;
- `target.key_expiry` (daily): the J-14/J-1 notices of a Kavita key, read again from the target;
- `intents.prune` (daily): the closed intents older than the retention leave the journal;
- `notify.dispatch` (EF-INT-006): the sender of the notification queue.

A job that finds nothing to do (no Suwayomi, no shelf, no keyring yet) returns quietly: a fresh
install runs every job and none of them touches anything.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
import structlog

from shelfhold.acquisition.public import FOLLOW_JOB_KIND, FollowCycle, bounded_receiver
from shelfhold.composition import Composition, KeyringUnavailableError, NoLibraryError
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.migration_detect import SwitchoverDetector, SwitchoverGate
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.application.preflight import PermissionPreflight
from shelfhold.export.application.reconcile import Reconciler
from shelfhold.export.targets.kavita.adopt import BulkParse
from shelfhold.export.targets.kavita.arrival import ArrivalPoller
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.infrastructure.observability.wiring import daily_access_audit
from shelfhold.infrastructure.schema import export
from shelfhold.integrations.apprise import NotificationDispatcher, register_notification_job
from shelfhold.jobs.huey_app import JobContext, JobPools
from shelfhold.library.public import PassthroughStore
from shelfhold.ops.events import BusTargetEvents

__all__ = [
    "EXPORT_JOB_KIND",
    "INTENT_PRUNE_KIND",
    "KEY_EXPIRY_KIND",
    "RECONCILE_HASH_KIND",
    "RECONCILE_KIND",
    "register_runtime_jobs",
]

log = structlog.get_logger(__name__)

EXPORT_JOB_KIND = "export.run"
RECONCILE_KIND = "reconcile.cycle"
RECONCILE_HASH_KIND = "reconcile.hash"
FOLLOW_CRON = "*/15 * * * *"
EXPORT_CRON = "*/5 * * * *"
RECONCILE_CRON = "*/10 * * * *"
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


def follow_handler(comp: Composition, pools: JobPools) -> Callable[[JobContext], None]:
    def run(_context: JobContext) -> None:
        link = comp.suwayomi()
        if link is None:
            return  # no connected service configured yet
        try:
            with comp.write_context() as w:
                store = PassthroughStore(
                    comp.engine, w.fsops, w.loaded.locate, comp.settings.data_dir,
                    tombstones=w.tombstones,
                )  # fmt: skip

                def place(_unit: str, variant_id: str, _filename: str) -> str:
                    return w.received_path(variant_id)

                cycle = FollowCycle(
                    comp.engine, w.bus, comp.clock, link.service, link.fetcher,
                    store=store,
                    receive=bounded_receiver(
                        link.guarded, staging_dir=comp.settings.data_dir / "tmp",
                        max_bytes=comp.settings.received_cbz_max_bytes,
                        authorize=getattr(link.fetcher, "authorize", None),  # the archive route
                    ),  # asks for the same credential as the other routes of the service
                    place=place, slots=pools.slots, tombstones=w.tombstones,
                )  # fmt: skip

                async def once() -> None:
                    try:
                        await cycle.run()
                    finally:
                        await link.guarded.aclose()

                asyncio.run(once())
        except (NoLibraryError, KeyringUnavailableError) as error:
            log.info("follow.cycle_skipped", reason=type(error).__name__)
            return
        pools.submit(EXPORT_JOB_KIND, dedupe_key=EXPORT_JOB_KIND)  # what arrived goes out

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
        gate=comp.gates.writable,
    )  # fmt: skip
    pipeline.use_preflight(PermissionPreflight(comp.engine, fsops, comp.bus, clock=comp.clock))
    pipeline.use_switchover(
        SwitchoverGate(comp.engine, SwitchoverDetector(comp.engine, fsops), clock=comp.clock)
    )
    return pipeline, reconciler, fsops


def _unchecked(_name: str) -> Any:
    raise KeyError("names are not checked by plugin/parse")


def export_handler(comp: Composition) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        destinations = tree_destinations(comp)
        if not destinations:
            return
        scans = ScanRelay()
        try:
            pipeline, _reconciler, _fsops = build_pipeline(comp, scans)
        except (NoLibraryError, KeyringUnavailableError) as error:
            log.info("export.run_skipped", reason=type(error).__name__)
            return
        for destination in destinations:
            if context.stop_requested:
                return
            try:
                client = comp.kavita_client(destination.target_id)
            except (KeyringUnavailableError, KeyError, sa.exc.NoResultFound):
                continue  # the target has no sealed key yet
            scans.client, scans.asked = client, []
            asyncio.run(_export_one(comp, pipeline, client, destination, scans, context))

    return run


async def _export_one(
    comp: Composition,
    pipeline: ExportPipeline,
    client: KavitaClient,
    destination: Destination,
    scans: ScanRelay,
    context: JobContext,
) -> None:
    from shelfhold.export.application.target_state import DestinationPausedError
    from shelfhold.export.targets.kavita.errors import TargetError

    try:
        bulk = await BulkParse.for_plan(client, pipeline, destination.id)
        pipeline.bind_parse(bulk, verified=True)
        pipeline.run(destination.id, job_id=context.job_id)
    except (DestinationPausedError, TargetError) as error:
        log.info(
            "export.destination_waits", destination_id=destination.id, reason=type(error).__name__
        )
        return
    finally:
        if scans.asked:  # the debounce: scans leave after a quiet moment
            deadline = time.monotonic() + SCAN_QUIET_S
            while time.monotonic() < deadline and not context.stop_requested:
                await asyncio.sleep(1)
            try:
                await client.flush_scans()
            except TargetError as error:  # Kavita lost after the writing: the next run scans again
                log.info("export.scan_failed", destination_id=destination.id,
                         reason=type(error).__name__)  # fmt: skip
    poller = ArrivalPoller(comp.engine, comp.clock, BusTargetEvents(comp.engine, comp.bus))
    try:
        await poller.poll(client, destination.id)
    except TargetError as error:  # one destination failing never stops the others
        log.info("export.poll_failed", destination_id=destination.id, reason=type(error).__name__)


# -- reconcile -----------------------------------------------------------------


def reconcile_handler(comp: Composition, *, in_window: bool) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        try:
            fsops, _loaded, _tombstones = comp.read_context()
        except (NoLibraryError, KeyringUnavailableError):
            return
        reconciler = Reconciler(comp.engine, fsops, clock=comp.clock, bus=comp.bus)
        for destination in tree_destinations(comp):
            if context.stop_requested:
                return
            reconciler.reconcile(destination.id, in_window=in_window)

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
    pools.scheduler.add(EXPORT_JOB_KIND, EXPORT_CRON, jitter_s=60)
    pools.register(
        RECONCILE_KIND, pool="io", handler=reconcile_handler(comp, in_window=False), writes=False
    )
    pools.scheduler.add(RECONCILE_KIND, RECONCILE_CRON, jitter_s=60)
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
