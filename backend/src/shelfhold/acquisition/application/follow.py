"""The follow cycle (EF-SRC-009, 025, 026 c, 027, 028; EF-INT-022).

Each cycle: wake the service if it sleeps behind a WakeHook, probe it, read its library, stop
following what left it, ingest the chapters of each followed series (a new one gets a download
ticket), advance the open tickets, then check freshness.

Two modes (EF-SRC-025). `always_on`: Suwayomi follows its own schedule, Shelfhold only reads it
and no refresh mutation is ever sent. `piloted`: Shelfhold asks Suwayomi to refresh the followed
series, at a polite pace with a jitter, before it reads the news.

A wake that fails puts the cycle off (`report.deferred`): the service is not at fault, so nothing
is counted (no content error, no outage) and `wake.deferred` follows after three deferred batches.

An error that says the service itself cannot be driven (authentication, version, schema) pauses
the whole service and counts against nothing; the pause ends with `resume`.
"""

import asyncio
import contextlib
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, NamedTuple, cast

import sqlalchemy as sa
import structlog
from structlog.contextvars import bound_contextvars

from shelfhold.acquisition.application.backfill import (
    confirmed_ceiling,
    estimate_with,
    within_ceiling,
)
from shelfhold.acquisition.application.discovery import (
    BOUND_PREFIX,
    Notices,
    SeriesDiscovery,
    digest,
)
from shelfhold.acquisition.application.failures import FailureLedger, from_service, pause_of
from shelfhold.acquisition.application.instance import check_instance
from shelfhold.acquisition.application.planning import (
    NOTHING_YET,
    highest_numbered,
    pending,
    set_watermark,
    watermark_of,
)
from shelfhold.acquisition.application.preference import TeamPreference, record_release
from shelfhold.acquisition.application.ranges import Keeper, RangeTickets, RangeUnit
from shelfhold.acquisition.application.staleness import stale_series
from shelfhold.acquisition.application.tickets import (
    MAX_ATTEMPTS,
    MAX_REQUEUE,
    STALL_AFTER,
    CycleAbandoned,
    DownloadTickets,
    OpenRequest,
    Placement,
    Receiver,
    Sleep,
    TicketReport,
)
from shelfhold.acquisition.domain.backfill import BackfillMode
from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.acquisition.domain.freshness import DEFAULT_K
from shelfhold.acquisition.domain.requests import (
    DEFAULT_KEY_CAP,
    DEFAULT_SERVICE_CAP,
    Mode,
    RequestBudget,
)
from shelfhold.acquisition.domain.team_policy import DEFAULT_INACTIVE_MONTHS
from shelfhold.acquisition.domain.watermark import Watermark
from shelfhold.catalog.public import (
    ARTIST,
    WRITER,
    BindingRef,
    Catalog,
    IncomingUnit,
    IngestOutcome,
    IngestResult,
    ingest_isolated,
    period_start,
    place_key,
    place_parts,
    stored_date,
)
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.export.public import attach_to_volume
from shelfhold.infrastructure.clock import Clock, FutureDateError, reject_future
from shelfhold.infrastructure.content_trust import sanitize_upstream_text
from shelfhold.infrastructure.follow_suspended import suspended_work_ids
from shelfhold.infrastructure.logging import elapsed_ms
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.infrastructure.wake import WakeDeferred, Wakes
from shelfhold.jobs.ledger import Created
from shelfhold.jobs.public import DomainSlots, JobContext, JobPools
from shelfhold.library.public import (
    EDITION_KEY_COLUMNS,
    PassthroughStore,
    PlaceRef,
    SeriesBindingRef,
    TombstoneGate,
    VariantRef,
    has_readable_copy,
)
from shelfhold.plugin_host.errors import (
    AuthModeUnsupported,
    ServiceInstanceChanged,
    ServiceVersionMismatch,
)
from shelfhold.plugin_host.refs_guard import screened, values_of
from shelfhold.sdk.connected import ConnectedCapability, ConnectedService
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.refs import SeriesRef, UnitInfo
from shelfhold.sdk.source import Capability, SourceError, SubSource

log = structlog.get_logger(__name__)

FOLLOW_JOB_KIND = "follow.cycle"

# What a cycle does only for a service that declares the capability (EF-SRC-033).
_OPTIONAL_STEPS: tuple[tuple[ConnectedCapability, str], ...] = (
    (ConnectedCapability.HEALTH_PROBE, "functional probe"),
    (ConnectedCapability.CATEGORIES, "category exclusion"),
    (ConnectedCapability.UPSTREAM_RESTORE, "upstream settings snapshot"),
)


def follow_dedupe_key(plugin_id: str) -> str:
    """One follow job per service at a time: a second submission joins the one in flight."""
    return f"{FOLLOW_JOB_KIND}:{plugin_id}"


def submit_follow(pools: JobPools, plugin_id: str) -> Created:
    return pools.submit(
        FOLLOW_JOB_KIND, dedupe_key=follow_dedupe_key(plugin_id), source_key=plugin_id
    )


MISSING_CYCLES = 2  # consecutive cycles of absence before a series is deemed removed
OUTAGE_CYCLES = 3  # cycles in a row without an answer before the service is "unavailable"
REFRESH_SPACING = 2.0  # seconds between two refreshes asked of the same upstream source
REFRESH_JITTER = 1.0  # plus up to this much, drawn at random


def refresh_delays(
    keys: list[str], *, spacing: float = REFRESH_SPACING, jitter: float = REFRESH_JITTER,
    draw: Callable[[float, float], float] = random.uniform,
) -> list[float]:  # fmt: skip
    """Seconds to wait before each refresh: none for the first of an upstream source (`limit_key`),
    `spacing` plus a jitter before the next ones, so one source is never hit in a burst."""
    seen: set[str] = set()
    delays = []
    for key in keys:
        delays.append(spacing + draw(0.0, jitter) if key in seen else 0.0)
        seen.add(key)
    return delays


@dataclass
class FollowReport:
    discovered: int = 0
    requested: int = 0
    fetched: int = 0
    removed: int = 0
    tombstoned: int = 0  # releases and series left alone because they were purged (EF-BIB-053)
    reactivated: int = 0
    created: int = 0  # series of the library given their Work, Edition and binding (EF-SRC-009)
    skipped_pending_pairing: int = 0  # left to `adopt pair`: an adopted twin waits for it
    unknown_language: int = 0  # filed as `x-unknown`: `edition set-lang` gives the language
    awaiting_shelf: int = 0  # no adult shelf to file them on
    stale: int = 0
    paused: str | None = None  # the waiting state (or failure class) the service is paused in
    unavailable: bool = False  # the service has not answered for OUTAGE_CYCLES cycles in a row
    deferred: bool = False  # the service sleeps and was not woken: the cycle is put off
    refreshed: int = 0  # series Suwayomi was asked to refresh (piloted mode)
    not_loaded: int = 0  # upstream sources the probe found unusable (EF-SRC-026 a)
    tickets: TicketReport = field(default_factory=TicketReport)

    def counters(self) -> dict[str, object]:
        """What the job line says of the cycle (numbers and a waiting state, never a title)."""
        return {
            "discovered": self.discovered, "requested": self.requested,
            "fetched": self.fetched, "removed": self.removed, "tombstoned": self.tombstoned,
            "reactivated": self.reactivated, "created": self.created,
            "awaiting_shelf": self.awaiting_shelf, "stale": self.stale,
            "paused": self.paused, "unavailable": self.unavailable,
            "deferred": self.deferred, "refreshed": self.refreshed,
            "not_loaded": self.not_loaded,
        }  # fmt: skip

    def has_news(self) -> bool:
        """Something changed or moved: the summary of the cycle is then worth an INFO line."""
        quiet = {"paused", "unavailable", "stale", "deferred"}
        return any(
            bool(value) for name, value in self.counters().items() if name not in quiet
        ) or bool(self.paused or self.unavailable)


class _Binding(NamedTuple):
    id: str
    edition_id: str
    work_id: str
    active: bool
    disabled_reason: str | None
    priority: int
    last_checked: datetime | None
    check_override: str | None
    source_key: str
    remote_id: str
    cap: int | None  # per-cycle cap of this series, in Units (None: only the shared caps)


@dataclass
class _Look:
    """What one listing of a series needs while its releases are ingested."""

    binding: _Binding
    mark: Watermark | None
    now: datetime
    contested: set[str]  # raw numbers offered more than once: elected once all are known
    taken: set[str] = field(default_factory=set)  # Variants whose slot was counted (a replay)
    touched: set[str] = field(default_factory=set)
    opened: set[str] = field(default_factory=set)


class FollowCycle:
    def __init__(
        self,
        engine: sa.Engine,
        bus: EventBus,
        clock: Clock,
        service: ConnectedService,
        fetcher: Fetcher,
        *,
        store: PassthroughStore,
        receive: Receiver,
        place: Placement,
        sleep: Sleep = asyncio.sleep,
        slots: DomainSlots | None = None,
        stale_k: int = DEFAULT_K,
        max_attempts: int = MAX_ATTEMPTS,
        stall_after: timedelta = STALL_AFTER,
        requeue_cap: int = MAX_REQUEUE,
        service_cap: int = DEFAULT_SERVICE_CAP,
        key_cap: int = DEFAULT_KEY_CAP,
        inactive_months: int = DEFAULT_INACTIVE_MONTHS,
        tombstones: TombstoneGate,
        keep_range: Keeper | None = None,
        wakes: Wakes | None = None,
        check_mode: Literal["always_on", "piloted"] = "always_on",
        refresh_delay: Callable[[list[str]], list[float]] = refresh_delays,
    ) -> None:
        self._engine = engine
        self._bus = bus
        self._clock = clock
        self._plugin_id = service.manifest.key
        # every reference the service hands back goes through the guard (EF-SRC-035): refused or
        # rewritten to the template the adapter declared, before the cycle stores any of it
        service = cast(
            "ConnectedService",
            screened(service, values_of(fetcher), lambda: getattr(fetcher, "ref_template", None)),
        )
        self._service = service
        self._fetcher = fetcher
        self._k = stale_k
        self._ledger = FailureLedger(self._plugin_id)
        self._tickets = DownloadTickets(
            engine, clock, service, fetcher, self._ledger, store, receive, place,
            plugin_id=self._plugin_id, sleep=sleep, slots=slots, max_attempts=max_attempts,
            stall_after=stall_after, announce=self._emit, requeue_cap=requeue_cap,
            queue_empty=self._server_queue_empty, blocked=self._request_blocked,
        )  # fmt: skip
        self._pending: list[PublishedEvent] = []
        self._caps = (service_cap, key_cap)
        self._prefs = TeamPreference(inactive_months=inactive_months)
        self._series_taken: dict[str, int] = {}
        self._tombstones = tombstones  # EF-BIB-053: required, a cycle never runs without it
        self._discovery = SeriesDiscovery(
            engine, clock, self._plugin_id, tombstones, self._emit,
            translation_nature=getattr(service.manifest, "translation_nature", "unknown"),
        )  # fmt: skip
        self._receive_files = receive
        self._keep_range = keep_range  # the host store of a ranged file (EF-SRC-032)
        self._ranged: dict[str, list[UnitInfo]] = {}  # listing of the ranged series, this cycle
        self._listed: dict[str, list[UnitInfo]] = {}  # discovered now: the listing already read
        self._notices = Notices(clock)
        self._buried: dict[str, str] = {}  # tombstoned series of this cycle, by notice key
        self._buried_ids: dict[str, dict[str, object]] = {}
        self._wakes = wakes  # EF-INT-022: the service may sleep behind a WakeHook
        self._piloted = check_mode == "piloted"  # EF-SRC-025 (a)
        self._refresh_delay = refresh_delay
        self._sleep = sleep

    # -- the cycle ---------------------------------------------------------------------------
    async def run(self) -> FollowReport:
        report = FollowReport()
        started = time.perf_counter()
        with self._engine.connect() as connection:
            paused = self._ledger.paused(connection)
        if paused is not None:
            report.paused = paused.label  # waiting for a human: no request, no second alert
            log.debug("follow.cycle_paused", state=paused.label, service=self._plugin_id)
            return report
        try:
            async with self._awake():
                await self._cycle(report)
            self._end_outage()
            # INFO only when the cycle changed something: a quiet cycle is one DEBUG line
            (log.info if report.has_news() else log.debug)(
                "follow.cycle_summary", duration_ms=elapsed_ms(started), **report.counters()
            )
        except WakeDeferred:
            # asleep and not woken in time: neither a failure of the source (L15 counts nothing)
            # nor an outage; the next cycle wakes it again, and `wake.deferred` follows
            log.info("follow.cycle_deferred", service=self._plugin_id)
            report.deferred = True
            return report
        except CycleAbandoned as error:
            log.warning("follow.cycle_abandoned", reason=str(error))
            return report  # no failure, no outage, no retry: the next cycle starts again
        except Exception as error:
            failure_class = classify(error)
            if disposition_of(failure_class) is not Disposition.PAUSE:
                log.warning("follow.cycle_interrupted", failure_class=failure_class)
                if failure_class == "infra":
                    self._note_outage(report)
                return report  # asleep, busy, unreachable: the next cycle tries again
            self._pause(failure_class, error, report)
        finally:
            self._flush()
        return report

    def _awake(self) -> contextlib.AbstractAsyncContextManager[None]:
        """The service awake for the whole cycle (before the batch and before the check), its
        session renewed while the cycle runs long; nothing at all without a WakeHook."""
        target = getattr(self._service, "wake_target", None)
        if (
            self._wakes is None
            or ConnectedCapability.WAKE not in self._service.connected
            or not target
        ):
            return contextlib.nullcontext()
        return self._wakes.session(target)

    def _note_outage(self, report: FollowReport) -> None:
        """A cycle that could not reach the service. A blip is nothing; `OUTAGE_CYCLES` in a row
        is an outage: `source.broken` (reason `unreachable`) once, counted against no series. The
        next cycle is the probe: the first that gets through ends the outage by itself."""
        with self._engine.begin() as connection:
            if self._ledger.bump_outage(connection) < OUTAGE_CYCLES:
                return
            report.unavailable = True
            if self._ledger.mark_unavailable(connection):
                self._emit(connection, "source.broken", {"reason": "unreachable"})

    def _end_outage(self) -> None:
        with self._engine.begin() as connection:
            self._ledger.clear_outage(connection)

    def resume(self) -> None:
        """The admin dealt with the cause (renewed the secret, updated the tool): follow again."""
        with self._engine.begin() as connection:
            self._ledger.resume(connection)
        self._tickets.reopen_abandoned()

    def _log_skipped_capabilities(self) -> None:
        """A step the service has no capability for is skipped, and said so (EF-SRC-033)."""
        for capability, step in _OPTIONAL_STEPS:
            if capability not in self._service.connected:
                log.debug(
                    "follow.capability_skipped",
                    capability=capability.name.lower() if capability.name else "?",
                    step=step,
                    service=self._plugin_id,
                )

    async def _check_instance(self) -> None:
        """Another installation of the tool pauses the follow, before anything is read from it
        (EF-SRC-036): its empty library must not look like series that were removed."""
        current = await from_service(self._service.instance_fingerprint(self._fetcher))
        with self._engine.begin() as connection:
            if check_instance(connection, self._plugin_id, current, self._clock.now()):
                raise ServiceInstanceChanged()

    async def _cycle(self, report: FollowReport) -> None:
        self._log_skipped_capabilities()
        if ConnectedCapability.HEALTH_PROBE in self._service.connected:
            health = await from_service(self._service.semantic_health(self._fetcher))
            if not health.ok:
                raise _probe_error(health.detail)
        if ConnectedCapability.INSTANCE_FINGERPRINT in self._service.connected:
            await self._check_instance()
        library = await from_service(self._service.followed_series(self._fetcher))
        log.debug("follow.library_listed", series=len(library), service=self._plugin_id)
        unloaded = await self._probe_sources(library, report)
        await self._discover_series(
            [ref for ref in library if ref.source_key not in unloaded], report
        )
        in_library = {(ref.source_key, ref.remote_id) for ref in library}
        present = self._reconcile_library(in_library, report)
        budget = RequestBudget(*self._caps)
        self._series_taken = {}
        self._buried = {}
        self._buried_ids = {}
        suspended = self._suspended()
        followed = [
            binding
            for binding in self._bindings()
            if binding.active
            and binding.work_id not in suspended  # adopted child library, unrated (EF-EXP-048)
            and binding.check_override != "paused"
            and binding.source_key not in unloaded  # degraded: asked nothing until it is back
            and (binding.source_key, binding.remote_id) in present
            and not self._series_tombstoned(binding, report)
        ]
        self._remember_buried()
        log.debug("follow.followed", followed=len(followed), in_library=len(present))
        if self._piloted:
            await self._refresh(followed, report)
        for binding in followed:
            await self._discover_isolated(binding, report, budget)
        self._fill(followed, budget, report)
        await self._ranges(followed, budget, report)
        report.tickets = await self._tickets.advance()
        report.fetched += report.tickets.fetched
        if report.tickets.fetched:  # the source delivers: what was given up may be tried again
            self._tickets.reopen_abandoned()
        if report.tickets.dropped:
            with self._engine.begin() as connection:
                self._emit(
                    connection, "source.downloads_dropped", {"count": report.tickets.dropped}
                )
        report.stale = len(self.check_freshness())

    async def _probe_sources(
        self, library: list[SeriesRef], report: FollowReport
    ) -> frozenset[str]:
        """EF-SRC-026 (a), read only: the upstream sources of the followed series that the service
        cannot use (missing, replaced by a stub, half updated). Each is told once per episode
        (`source.not_loaded`); its series are left alone (no listing, no request, no requeue)
        until it is loaded again, and what was given up meanwhile is then asked once more. It is
        never a content error (EF-BIB-042): nothing is counted against the source."""
        if ConnectedCapability.HEALTH_PROBE not in self._service.connected:
            return frozenset()
        keys = {ref.source_key for ref in library}
        keys |= {b.source_key for b in self._bindings() if b.active}
        found = await from_service(self._service.unloaded_sources(self._fetcher, sorted(keys)))
        unloaded = frozenset(found) & keys
        with self._engine.begin() as connection:
            known = self._ledger.not_loaded(connection)
            told = 0
            for key in sorted(unloaded - known):
                if self._ledger.mark_not_loaded(connection, key):
                    told += 1
            if told:  # one event for the cycle: the sources are named by `status`, not by an event
                self._emit(connection, "source.not_loaded", {"count": told})
            back = (known - unloaded) & keys
            for key in sorted(back):
                self._ledger.clear_not_loaded(connection, key)
        if back:
            self._tickets.reopen_abandoned(back)
        self._tickets.unloaded = unloaded
        report.not_loaded = len(unloaded)
        return unloaded

    async def _server_queue_empty(self) -> bool:
        """EF-SRC-026 (b): the service's download queue holds nothing (a service that cannot say
        is never taken for empty)."""
        if ConnectedCapability.HEALTH_PROBE not in self._service.connected:
            return False
        return await from_service(self._service.download_queue_size(self._fetcher)) == 0

    def _request_blocked(self, request: OpenRequest) -> bool:
        """A chapter, or the series it belongs to, was purged for good: never asked again."""
        refs = (
            VariantRef(self._plugin_id, request.source_key, request.remote_unit_id),
            SeriesBindingRef(self._plugin_id, request.source_key, request.remote_id),
        )
        return any(self._tombstones.check(ref, root_id=None).blocked for ref in refs)

    async def _refresh(self, followed: list[_Binding], report: FollowReport) -> None:
        """Piloted mode: Suwayomi is asked to look at each followed series upstream, one source at
        a time with a spacing and a jitter, and the cycle reads the news once each has answered.
        A refresh that fails is that series' problem and counts nothing: it is not a content error,
        and what Suwayomi already knows is read anyway. An error that says the whole service
        cannot be driven goes up and pauses."""
        delays = self._refresh_delay([b.source_key for b in followed])
        for binding, delay in zip(followed, delays, strict=True):
            if delay:
                await self._sleep(delay)
            ref = SeriesRef(source_key=binding.source_key, remote_id=binding.remote_id, title="")
            try:
                await from_service(self._service.request_refresh(self._fetcher, [ref]))
                report.refreshed += 1
            except Exception as error:
                failure_class = classify(error)
                if disposition_of(failure_class) is Disposition.PAUSE:
                    raise
                log.warning("follow.refresh_skipped", binding_id=binding.id,
                            failure_class=failure_class)  # fmt: skip

    def _is_ranged(self) -> bool:
        return (
            self._keep_range is not None
            and Capability.RANGED_CONTENT in self._service.manifest.capabilities
            and ConnectedCapability.SERVER_QUEUE in self._service.connected
        )

    async def _ranges(
        self, followed: list[_Binding], budget: RequestBudget, report: FollowReport
    ) -> None:
        """A `ranged_content` service delivers one file per volume: plan, ask, advance."""
        if self._keep_range is None:
            return
        v = catalog.unit_variant
        for binding in followed:
            infos = self._ranged.pop(binding.id, None)
            if infos is None:
                continue
            with self._engine.connect() as connection:
                by_remote = {
                    r.remote_unit_id: r.unit_id
                    for r in connection.execute(
                        sa.select(v.c.remote_unit_id, v.c.unit_id).where(
                            v.c.source_binding_id == binding.id
                        )
                    )
                }
            units = [
                RangeUnit(by_remote[i.remote_unit_id], i)
                for i in infos
                if i.remote_unit_id in by_remote
            ]
            ref = SeriesRef(source_key=binding.source_key, remote_id=binding.remote_id, title="")
            tickets = RangeTickets(
                self._engine, self._clock, self._service, self._fetcher, self._receive_files,
                self._keep_range, edition_id=binding.edition_id, binding_id=binding.id, series=ref,
            )  # fmt: skip
            for group in tickets.plan(units):
                tickets.request(group, budget)
            ru, rq = acquisition.download_request_unit, acquisition.download_request
            with self._engine.connect() as connection:  # EF-SRC-026 b: only what was not covered
                partial = (
                    connection.execute(
                        sa.select(ru.c.download_request_id)
                        .join(rq, rq.c.id == ru.c.download_request_id)
                        .where(rq.c.source_binding_id == binding.id, ru.c.state == "missing")
                        .distinct()
                    )
                    .scalars()
                    .all()
                )
            for request_id in partial:
                tickets.retry_uncovered(request_id, budget)
            done = await tickets.advance()
            report.fetched += done.fetched

    async def _discover_series(self, library: list[SeriesRef], report: FollowReport) -> None:
        """EF-SRC-009: a series of the library without a binding gets its Work, Edition and
        binding before anything else is read; its watermark is its highest visible chapter."""
        found = await self._discovery.run(
            library,
            lambda ref: from_service(self._service.list_units(self._fetcher, ref)),
            self._sub_sources,
        )
        self._listed = found.units
        report.created += found.created
        report.tombstoned += found.tombstoned
        report.skipped_pending_pairing += found.skipped_pending_pairing
        report.unknown_language += found.created_unknown_language
        report.awaiting_shelf += found.awaiting_shelf

    async def _sub_sources(self) -> list[SubSource]:
        """The sub-sources of the service; none for a service that has no such capability (the
        method exists only when the manifest declares it)."""
        listing = getattr(self._service, "sub_sources", None)
        return [] if listing is None else await from_service(listing(self._fetcher))

    def _reconcile_library(
        self, in_library: set[tuple[str, str]], report: FollowReport
    ) -> set[tuple[str, str]]:
        """Stop following what left the library, but never on a single look and never in bulk.

        A series must be absent in two consecutive cycles (a restarting tool, a half-loaded
        library or a reinstall answers for a moment with less than it has). When the library is
        empty while series are followed, or when every followed series (two or more) vanishes at
        once, that is the tool, not the user: nothing is deactivated and the admin is alerted.
        """
        bindings = self._bindings()
        active = [b for b in bindings if b.active]
        absent = [b for b in active if (b.source_key, b.remote_id) not in in_library]
        mass = bool(absent) and (
            not in_library or (len(active) >= 2 and len(absent) == len(active))
        )
        with self._engine.begin() as connection:
            if mass:
                if self._ledger.raise_mass_alert(connection):
                    self._emit(
                        connection, "source.instance_changed",
                        {"state": "source_instance_changed", "code": "source.instance_changed"},
                    )  # fmt: skip
            else:
                self._ledger.clear_mass_alert(connection)
        if not mass:
            for binding in active:
                if binding in absent:
                    with self._engine.begin() as connection:
                        count = self._ledger.bump_missing(connection, binding.id)
                    if count >= MISSING_CYCLES and self._stop_following(binding):
                        report.removed += 1
                else:
                    with self._engine.begin() as connection:
                        self._ledger.clear_missing(connection, binding.id)
        for binding in bindings:
            if (
                not binding.active
                and (binding.source_key, binding.remote_id) in in_library
                and self._reactivate(binding)
            ):
                report.reactivated += 1
        return in_library

    def _series_tombstoned(self, binding: _Binding, report: FollowReport) -> bool:
        """A series purged for good is ignored, whatever Suwayomi still lists: no Work created, no
        chapter asked. It is told once (`series.tombstoned_upstream`), the admin removes it from
        Suwayomi by hand (EF-BIB-053 (2), M1a)."""
        ref = SeriesBindingRef(self._plugin_id, binding.source_key, binding.remote_id)
        if not self._tombstones.check(ref, root_id=None).blocked:
            return False
        log.debug("follow.series_tombstoned", binding_id=binding.id, edition_id=binding.edition_id)
        report.tombstoned += 1
        key = BOUND_PREFIX + digest(binding.id)
        self._buried[key] = self._title_of(binding.work_id)
        self._buried_ids[key] = {"edition_id": binding.edition_id, "work_id": binding.work_id}
        return True

    def _title_of(self, work_id: str) -> str:
        with self._engine.connect() as connection:
            title = connection.execute(
                sa.select(catalog.work.c.title).where(catalog.work.c.id == work_id)
            ).scalar()
        return str(title or "")

    def _remember_buried(self) -> None:
        """What `shelfhold status` lists as "tombstoned upstream" until the admin removes the
        series from Suwayomi (the rows of the series that already have a binding)."""
        with self._engine.begin() as connection:
            fresh = self._notices.sync(
                connection, "tombstoned", self._buried,
                own=lambda key: key.startswith(BOUND_PREFIX),
            )  # fmt: skip
            for key in fresh:  # the notice is the memory: told once, whatever the processes
                self._emit(connection, "series.tombstoned_upstream", self._buried_ids[key])

    def _release_tombstoned(self, binding: _Binding, info: UnitInfo, key: dict[str, str]) -> bool:
        """A release that matches a tombstone is never ingested: not by its reference (`variant`),
        not at the place of a purged chapter under a new identifier (`place`)."""
        track, segment, part = place_parts(info.number, info.source_order)
        candidates = (
            VariantRef(self._plugin_id, binding.source_key, info.remote_unit_id),
            PlaceRef.of(
                key, track=track, number_segment=segment, number_raw=info.number or "", part=part
            ),
        )  # fmt: skip
        blocked = any(self._tombstones.check(c, root_id=None).blocked for c in candidates)
        if blocked:
            log.debug("follow.release_tombstoned", remote_unit_id=info.remote_unit_id)
        return blocked

    def _edition_key(self, edition_id: str) -> dict[str, str]:
        e = catalog.edition
        with self._engine.connect() as connection:
            row = connection.execute(select_key(e, edition_id)).one()
        return {k: str(getattr(row, k)) for k in EDITION_KEY_COLUMNS}

    # -- bindings ----------------------------------------------------------------------------
    def _suspended(self) -> frozenset[str]:
        """Works whose following an adoption suspended until an admin rates them."""
        with self._engine.connect() as connection:
            return suspended_work_ids(connection)

    def _bindings(self) -> list[_Binding]:
        b, si, f = acquisition.source_binding, acquisition.source_item, acquisition.series_follow
        query = (
            sa.select(
                b.c.id, b.c.edition_id, b.c.work_id, b.c.active, b.c.disabled_reason,
                b.c.priority, b.c.last_checked, f.c.check_override, si.c.source_key,
                si.c.remote_id, f.c.cap,
            )
            .select_from(
                b.join(si, si.c.id == b.c.source_item_id).join(
                    f, f.c.edition_id == b.c.edition_id
                )
            )
            .where(
                si.c.plugin_id == self._plugin_id,
                sa.or_(b.c.active, b.c.disabled_reason == "removed_upstream"),
            )
            .order_by(b.c.edition_id, b.c.priority)
        )  # fmt: skip
        with self._engine.connect() as connection:
            return [_Binding._make(row) for row in connection.execute(query)]

    def _stop_following(self, binding: _Binding) -> bool:
        """EF-SRC-027: the series left the Suwayomi library. The binding goes inactive, so its
        Units are irreplaceable by calculation; no file and no follow row is touched."""
        table = acquisition.source_binding
        with self._engine.begin() as connection:
            changed = connection.execute(
                sa.update(table)
                .where(table.c.id == binding.id, table.c.active)
                .values(active=False, disabled_reason="removed_upstream")
            ).rowcount
            if changed:  # a concurrent cycle that got there first already announced it
                self._emit(
                    connection, "series.removed_upstream",
                    {"edition_id": binding.edition_id, "work_id": binding.work_id},
                )  # fmt: skip
            self._ledger.clear_missing(connection, binding.id)
        return bool(changed)

    def _reactivate(self, binding: _Binding) -> bool:
        table = acquisition.source_binding
        with self._engine.begin() as connection:
            taken = connection.execute(
                sa.select(table.c.id).where(
                    table.c.edition_id == binding.edition_id,
                    table.c.priority == binding.priority,
                    table.c.active,
                )
            ).first()
            if taken is not None:  # another binding took its rank meanwhile: M1b decides
                return False
            return bool(
                connection.execute(
                    sa.update(table)
                    .where(table.c.id == binding.id, sa.not_(table.c.active))
                    .values(active=True, disabled_reason=None)
                ).rowcount
            )

    # -- discovery ---------------------------------------------------------------------------
    async def _discover_isolated(
        self, binding: _Binding, report: FollowReport, budget: RequestBudget
    ) -> None:
        """One series failing is that series' problem: the others are still followed. An error
        that says the whole service cannot be driven is the exception: it goes up and pauses."""
        try:
            with bound_contextvars(
                series_id=binding.work_id, edition_id=binding.edition_id, binding_id=binding.id
            ):
                await self._discover(binding, report, budget)
        except Exception as error:
            failure_class = classify(error)
            disposition = disposition_of(failure_class)
            if disposition is Disposition.PAUSE:
                raise
            if disposition is Disposition.COUNT:
                with self._engine.begin() as connection:
                    self._ledger.count_content(
                        connection, binding.id, binding.source_key,
                        getattr(error, "code", "source.content_removed"), self._clock.now(),
                    )  # fmt: skip
            log.warning("follow.series_skipped", binding_id=binding.id, failure_class=failure_class)

    async def _discover(
        self, binding: _Binding, report: FollowReport, budget: RequestBudget
    ) -> None:
        ref = SeriesRef(source_key=binding.source_key, remote_id=binding.remote_id, title="")
        units = self._listed.pop(binding.id, None)  # a series just discovered: read once
        fresh = units is not None
        if units is None:
            units = await from_service(self._service.list_units(self._fetcher, ref))
        if self._is_ranged():
            self._ranged[binding.id] = units
        await self._credit_authors(binding, ref)
        with self._engine.connect() as connection:
            mark = watermark_of(connection, binding.edition_id)
        if fresh:
            mark = None  # just discovered: this look is its baseline, nothing is asked (extras too)
        # Without a watermark the first look only learns what is there (it becomes the watermark):
        # a series followed from now on is asked for what is new, never for its whole archive.
        look = _Look(binding, mark, self._clock.now(), _contested(units))
        key = self._edition_key(binding.edition_id)
        for info in units:
            if self._release_tombstoned(binding, info, key):
                report.tombstoned += 1
                continue
            info = _trusted(info, look.now)  # text and date: nothing raw goes further (ENF-SEC-015)
            result = ingest_isolated(
                self._engine,
                IncomingUnit(
                    binding=BindingRef(binding.id, binding.edition_id),
                    remote_unit_id=info.remote_unit_id, raw_number=info.number, seen_at=look.now,
                    source_order=info.source_order, team_label=info.team, lang=info.lang,
                    title=info.title, published_at=info.published_at, kind=info.kind,
                    published_on=info.published_on, date_precision=info.date_precision,
                ),
                then=self._hook(info, look, budget),
            )  # fmt: skip
            report.discovered += int(result.outcome is IngestOutcome.NEW_VARIANT)
            log.debug(
                "follow.unit_seen",
                unit_id=result.unit_id,
                variant_id=result.variant_id,
                outcome=result.outcome.name.lower(),
            )
        for unit_id in sorted(look.touched):  # every offer is known now: one election per Unit
            with self._engine.begin() as connection:
                self._prefs.elect(connection, unit_id, look.now)
        report.requested += len(look.opened)
        log.debug(
            "follow.series_looked",
            units=len(units),
            requested=len(look.opened),
            baseline=mark is None,
            fresh=fresh,
        )
        with self._engine.begin() as connection:
            top = highest_numbered(connection, binding.edition_id) if mark is None else None
            if top == NOTHING_YET and fresh:
                top = None  # a discovered series with nothing numbered: the next look takes it
            if top is not None:
                set_watermark(connection, binding.edition_id, top, baseline_at=look.now)
            table = acquisition.source_binding
            connection.execute(
                sa.update(table).where(table.c.id == binding.id).values(last_checked=look.now)
            )

    async def _credit_authors(self, binding: _Binding, ref: SeriesRef) -> None:
        """The authors the source gives become contributors of the Work (EF-BIB-046).

        Asked while the Work has none: what is credited is never rewritten, and a series nobody
        signs is asked again, which costs one cached read. Text is made safe before it is stored
        (ENF-SEC-015). A source that cannot answer is no reason to skip the series' chapters.
        """
        with self._engine.connect() as connection:
            if Catalog(connection).work_is_credited(binding.work_id):
                return
        try:
            info = await from_service(self._service.get_series(self._fetcher, ref))
        except Exception as error:
            failure_class = classify(error)
            if disposition_of(failure_class) is Disposition.PAUSE:
                raise
            log.warning(
                "follow.authors_skipped", binding_id=binding.id, failure_class=failure_class
            )
            return
        with self._engine.begin() as connection:
            catalog_ = Catalog(connection)
            for role, names in ((WRITER, info.authors), (ARTIST, info.artists)):
                clean = [text for text in map(_plain, names) if text]
                if clean:
                    catalog_.credit_work(binding.work_id, role, clean)

    def _hook(
        self, info: UnitInfo, look: _Look, budget: RequestBudget
    ) -> Callable[[sa.Connection, IngestResult], None]:
        def then(connection: sa.Connection, result: IngestResult) -> None:
            self._on_ingest(connection, result, info, look, budget)

        return then

    def _on_ingest(
        self,
        connection: sa.Connection,
        result: IngestResult,
        info: UnitInfo,
        look: _Look,
        budget: RequestBudget,
    ) -> None:
        """In the ingest's own transaction: a Variant never exists without its ticket, unless the
        caps leave no room (the next cycle asks) or another team offers the same chapter (elected
        once the whole listing is read). A chapter named `Vol.N` that an adopted volume N holds is
        under that volume (EF-BIB-044): nothing is asked for it."""
        if info.volume_label and attach_to_volume(
            connection, look.binding.edition_id, result.unit_id, info.volume_label
        ):
            return
        if result.outcome not in (IngestOutcome.NEW_VARIANT, IngestOutcome.SUPERSEDING):
            return
        record_release(connection, result.team_id, info.published_at or look.now)
        if _place_of(info) in look.contested:
            look.touched.add(result.unit_id)
            return
        election = self._prefs.elect(connection, result.unit_id, look.now)
        if (
            result.outcome is not IngestOutcome.NEW_VARIANT  # a human decides a replacement
            or look.mark is None
            or election is None
            or election.variant_id != result.variant_id
            or not _is_new(connection, result.unit_id, look.mark)
        ):
            return
        if result.variant_id not in look.taken:
            if not self._take(look.binding, budget):
                return  # no room this cycle: asked next cycle, in order
            look.taken.add(result.variant_id)
        if DownloadTickets.open(
            connection, binding_id=look.binding.id, variant_id=result.variant_id,
            unit_id=result.unit_id, now=look.now,
        ):  # fmt: skip
            look.opened.add(result.variant_id)

    # -- requests beyond the first look: what the caps held back, then backfill -----------------
    def _take(self, binding: _Binding, budget: RequestBudget) -> bool:
        taken = self._series_taken.get(binding.id, 0)
        room = None if binding.cap is None else binding.cap - taken
        if room is not None and room <= 0:
            return False
        if budget.take(binding.source_key, 1, series_cap=room) == 0:
            log.debug("follow.budget_exhausted", binding_id=binding.id)
            return False
        self._series_taken[binding.id] = taken + 1
        return True

    def _fill(self, followed: list[_Binding], budget: RequestBudget, report: FollowReport) -> None:
        """New chapters first, then backfill; one Unit per series in turn so that no series
        starves another, all within the caps (EF-EXP-053)."""
        by_id = {binding.id: binding for binding in followed}
        editions = list({b.edition_id: b for b in followed}.values())
        report.requested += self._round_robin(editions, "follow", by_id, budget)
        with self._engine.connect() as connection:
            modes = {
                row.edition_id: row.backfill_mode
                for row in connection.execute(
                    sa.select(
                        acquisition.series_follow.c.edition_id,
                        acquisition.series_follow.c.backfill_mode,
                    )
                )
            }
        backfills: tuple[BackfillMode, ...] = ("gaps", "all")
        for mode in backfills:
            asked = [
                b
                for b in editions
                if modes.get(b.edition_id) == mode and self._backfill_ok(b, mode)
            ]
            report.requested += self._round_robin(asked, mode, by_id, budget)
        with self._engine.begin() as connection:
            for edition in editions:
                if modes.get(edition.edition_id) in ("gaps", "all") and not pending(
                    connection, edition.edition_id, modes[edition.edition_id]
                ):  # nothing left to bring back: the backfill is done
                    connection.execute(
                        sa.update(acquisition.series_follow)
                        .where(acquisition.series_follow.c.edition_id == edition.edition_id)
                        .values(backfill_mode="none")
                    )

    def _backfill_ok(self, edition: _Binding, mode: BackfillMode) -> bool:
        """Later arrivals may not take a backfill past what the user confirmed: it stops, and a
        new estimate must be confirmed."""
        with self._engine.begin() as connection:
            estimate = estimate_with(connection, edition.edition_id, mode, free_bytes=0)
            if within_ceiling(estimate, confirmed_ceiling(connection, edition.edition_id)):
                return True
            connection.execute(
                sa.update(acquisition.series_follow)
                .where(acquisition.series_follow.c.edition_id == edition.edition_id)
                .values(backfill_mode="none")
            )
        log.warning(
            "backfill.confirmation_required", edition_id=edition.edition_id, units=estimate.units
        )
        return False

    def _round_robin(
        self,
        editions: list[_Binding],
        mode: Mode,
        by_id: dict[str, _Binding],
        budget: RequestBudget,
    ) -> int:
        queues: dict[str, deque[str]] = {}
        with self._engine.connect() as connection:
            for edition in editions:
                units = pending(connection, edition.edition_id, mode)
                queues[edition.edition_id] = deque(u.unit_id for u in units)
        opened = 0
        while queues and budget.remaining > 0:
            for edition_id in list(queues):
                if not queues[edition_id]:
                    del queues[edition_id]
                    continue
                unit_id = queues[edition_id].popleft()
                outcome = self._open_for_unit(unit_id, by_id, budget)
                if outcome == "exhausted":
                    del queues[edition_id]
                opened += outcome == "opened"
        return opened

    def _open_for_unit(
        self, unit_id: str, by_id: dict[str, _Binding], budget: RequestBudget
    ) -> str:
        v = catalog.unit_variant
        now = self._clock.now()
        with self._engine.begin() as connection:
            chosen = connection.execute(
                sa.select(v.c.id, v.c.source_binding_id).where(
                    v.c.unit_id == unit_id, v.c.preferred, v.c.removed_at.is_(None)
                )
            ).first()
            variant_id, binding_id = (
                (chosen.id, chosen.source_binding_id) if chosen else (None, None)
            )
            if variant_id is None:
                election = self._prefs.elect(connection, unit_id, now)
                if election is not None:
                    variant_id, binding_id = election.variant_id, election.binding_id
            binding = by_id.get(binding_id) if binding_id else None
            if variant_id is None or binding is None:
                return "skipped"  # no live Variant, or it belongs to another service's binding
            if not self._take(binding, budget):
                return "exhausted"
            opened = DownloadTickets.open(
                connection, binding_id=binding.id, variant_id=variant_id, unit_id=unit_id, now=now
            )
        return "opened" if opened else "skipped"

    # -- freshness (EF-SRC-026 c) ------------------------------------------------------------
    def check_freshness(self) -> list[str]:
        """Editions newly stale (announced once per episode, `series.stale`)."""
        try:
            return self._check_freshness()
        finally:
            self._flush()

    def _check_freshness(self) -> list[str]:
        """One `series.stale` for the cycle, however many series went silent: it carries their
        number (an event holds no title). The ones announced stay in the ledger until they speak
        again, so a series is told once per episode; `shelfhold status` lists them all."""
        now = self._clock.now()
        with self._engine.begin() as connection:
            silent = stale_series(connection, now, k=self._k, plugin_id=self._plugin_id)
            fresh = self._ledger.sync_stale(connection, [str(s.edition_id) for s in silent])
            if fresh:
                self._emit(connection, "series.stale", {"count": len(fresh)})
        return fresh

    # -- pause and events --------------------------------------------------------------------
    def _pause(self, failure_class: str, error: Exception, report: FollowReport) -> None:
        pause = pause_of(failure_class, error)
        with self._engine.begin() as connection:
            if self._ledger.pause(connection, pause, self._clock.now()):
                payload: dict[str, object] = {"code": pause.event_type}
                if pause.waiting_state:
                    payload["state"] = pause.waiting_state
                self._emit(connection, pause.event_type, payload)
        report.paused = pause.label

    def _emit(self, connection: sa.Connection, event_type: str, payload: dict[str, object]) -> None:
        self._pending.append(self._bus.publish(connection, event_type, payload))

    def _flush(self) -> None:
        """Hand the events to in-process subscribers, after the transactions that wrote them."""
        pending, self._pending = self._pending, []
        self._bus.deliver(pending)


def select_key(edition: sa.Table, edition_id: str) -> sa.Select[tuple[object, ...]]:
    return sa.select(*(edition.c[k] for k in EDITION_KEY_COLUMNS)).where(edition.c.id == edition_id)


def _plain(text: str | None) -> str | None:
    return (sanitize_upstream_text(text) or None) if text else None


def _trusted(info: UnitInfo, now: datetime) -> UnitInfo:
    """What a source claims, made safe before it is stored or shown to Kavita.

    The title and the team name are plain text (ENF-SEC-015: no markup, no control or bidi
    character); an upload date later than the server's own clock allows is not believed
    (ENF-TIME-001): without it the receipt time stands, and a team dated 2099 still goes inactive.
    """
    published = info.published_at
    if published is not None:
        try:
            reject_future(published, now=now)
        except (FutureDateError, ValueError):  # ValueError: a date without a time zone
            log.warning("follow.future_date_ignored", remote_unit_id=info.remote_unit_id)
            published = None
    return info.model_copy(
        update={
            "number": _plain(info.number),
            "title": _plain(info.title),
            "team": _plain(info.team),
            "published_at": published,
            **_trusted_partial(info, now),
        }
    )


def _trusted_partial(info: UnitInfo, now: datetime) -> dict[str, str | None]:
    """A partial date the source gives (`2023`, `2023-05`): believed only if it is well formed,
    no finer than it says, and not later than the server's own clock allows (ENF-TIME-001)."""
    if info.published_on is None:
        return {"published_on": None, "date_precision": None}
    kept = stored_date(info.published_on, info.date_precision, None)
    if kept is None:
        log.warning("follow.partial_date_ignored", remote_unit_id=info.remote_unit_id)
        return {"published_on": None, "date_precision": None}
    try:
        reject_future(period_start(kept[0]), now=now)
    except FutureDateError:
        log.warning("follow.future_date_ignored", remote_unit_id=info.remote_unit_id)
        return {"published_on": None, "date_precision": None}
    return {"published_on": kept[0], "date_precision": kept[1]}


def _place_of(info: UnitInfo) -> str:
    """The normalized place of a release ("10" and "10.0" are one), never its raw text."""
    return place_key(info.number, info.source_order)


def _contested(units: list[UnitInfo]) -> set[str]:
    """Raw numbers that more than one release offers in this listing (another team's version)."""
    seen: set[str] = set()
    twice: set[str] = set()
    for info in units:
        key = _place_of(info)
        (twice if key in seen else seen).add(key)
    return twice


def _is_new(connection: sa.Connection, unit_id: str, mark: Watermark) -> bool:
    """A Unit worth asking for: beyond the watermark (an extra without a number is always new),
    not suppressed, and not already carried by a file or a volume (EF-BIB-044, EF-EXP-018)."""
    unit = catalog.unit
    row = connection.execute(
        sa.select(
            unit.c.number_segment_ordinal,
            unit.c.number_norm,
            unit.c.track,
            unit.c.follow_suppressed,
        ).where(unit.c.id == unit_id)
    ).one()
    if row.follow_suppressed or has_readable_copy(connection, unit_id):
        return False
    if row.track == "unnumbered":
        return True
    return mark.is_beyond(ordinal=row.number_segment_ordinal, number=Decimal(row.number_norm))


def _probe_error(detail: str | None) -> SourceError:
    if detail == "source.auth_mode_unsupported":
        return AuthModeUnsupported()
    return ServiceVersionMismatch(detail or "the service failed its probe")


def follow_job(make_cycle: Callable[[], FollowCycle]) -> Callable[[JobContext], None]:
    """The Huey handler of the periodic `follow.cycle` (registered by the composition root)."""

    def handler(context: JobContext) -> None:
        context.record(**asyncio.run(make_cycle().run()).counters())

    return handler
