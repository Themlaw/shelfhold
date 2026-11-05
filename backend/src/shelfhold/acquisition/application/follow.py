"""The always-on follow cycle (EF-SRC-009, 025, 026 c, 027, 028).

Suwayomi follows its own schedule; Shelfhold only reads it. Each cycle: probe the service, read
its library, stop following what left it, ingest the chapters of each followed series (a new one
gets a download ticket), advance the open tickets, then check freshness. No refresh mutation is
ever sent in this mode: the piloted mode that sends one belongs to M1b.

An error that says the service itself cannot be driven (authentication, version, schema) pauses
the whole service and counts against nothing; the pause ends with `resume`.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NamedTuple

import sqlalchemy as sa
import structlog

from shelfhold.acquisition.application.failures import FailureLedger, from_service, pause_of
from shelfhold.acquisition.application.tickets import (
    MAX_ATTEMPTS,
    STALL_AFTER,
    DownloadTickets,
    Placement,
    Receiver,
    Sleep,
    TicketReport,
)
from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.acquisition.domain.freshness import DEFAULT_K, is_stale
from shelfhold.catalog.public import (
    BindingRef,
    IncomingUnit,
    IngestOutcome,
    IngestResult,
    ingest_isolated,
)
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import acquisition, catalog, platform
from shelfhold.jobs.ledger import Created
from shelfhold.jobs.public import DomainSlots, JobContext, JobPools
from shelfhold.library.public import PassthroughStore
from shelfhold.plugin_host.errors import AuthModeUnsupported, ServiceVersionMismatch
from shelfhold.sdk.connected import ConnectedCapability, ConnectedService
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import SourceError

log = structlog.get_logger(__name__)

FOLLOW_JOB_KIND = "follow.cycle"


def follow_dedupe_key(plugin_id: str) -> str:
    """One follow job per service at a time: a second submission joins the one in flight."""
    return f"{FOLLOW_JOB_KIND}:{plugin_id}"


def submit_follow(pools: JobPools, plugin_id: str) -> Created:
    return pools.submit(
        FOLLOW_JOB_KIND, dedupe_key=follow_dedupe_key(plugin_id), source_key=plugin_id
    )


MISSING_CYCLES = 2  # consecutive cycles of absence before a series is deemed removed
FINISHED_STATUSES = ("completed", "cancelled")  # work status: nothing more is expected
NOT_CHECKED = ("completed", "paused")  # series_follow.check_override: the user said so


@dataclass
class FollowReport:
    discovered: int = 0
    requested: int = 0
    fetched: int = 0
    removed: int = 0
    reactivated: int = 0
    stale: int = 0
    paused: str | None = None  # the waiting state (or failure class) the service is paused in
    tickets: TicketReport = field(default_factory=TicketReport)


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
    ) -> None:
        self._engine = engine
        self._bus = bus
        self._clock = clock
        self._service = service
        self._fetcher = fetcher
        self._plugin_id = service.manifest.key
        self._k = stale_k
        self._ledger = FailureLedger(self._plugin_id)
        self._tickets = DownloadTickets(
            engine, clock, service, fetcher, self._ledger, store, receive, place,
            plugin_id=self._plugin_id, sleep=sleep, slots=slots, max_attempts=max_attempts,
            stall_after=stall_after,
        )  # fmt: skip
        self._pending: list[PublishedEvent] = []

    # -- the cycle ---------------------------------------------------------------------------
    async def run(self) -> FollowReport:
        report = FollowReport()
        with self._engine.connect() as connection:
            paused = self._ledger.paused(connection)
        if paused is not None:
            report.paused = paused.label  # waiting for a human: no request, no second alert
            return report
        try:
            await self._cycle(report)
        except Exception as error:
            failure_class = classify(error)
            if disposition_of(failure_class) is not Disposition.PAUSE:
                log.warning("follow.cycle_interrupted", failure_class=failure_class)
                return report  # asleep, busy, unreachable: the next cycle tries again
            self._pause(failure_class, error, report)
        finally:
            self._flush()
        return report

    def resume(self) -> None:
        """The admin dealt with the cause (renewed the secret, updated the tool): follow again."""
        with self._engine.begin() as connection:
            self._ledger.resume(connection)
        self._tickets.reopen_abandoned()

    async def _cycle(self, report: FollowReport) -> None:
        if ConnectedCapability.HEALTH_PROBE in self._service.connected:
            health = await from_service(self._service.semantic_health(self._fetcher))
            if not health.ok:
                raise _probe_error(health.detail)
        in_library = {
            (ref.source_key, ref.remote_id)
            for ref in await from_service(self._service.followed_series(self._fetcher))
        }
        present = self._reconcile_library(in_library, report)
        for binding in self._bindings():
            if (
                binding.active
                and binding.check_override != "paused"
                and (binding.source_key, binding.remote_id) in present
            ):
                await self._discover_isolated(binding, report)
        report.tickets = await self._tickets.advance()
        report.fetched = report.tickets.fetched
        if report.tickets.fetched:  # the source delivers: what was given up may be tried again
            self._tickets.reopen_abandoned()
        if report.tickets.dropped:
            with self._engine.begin() as connection:
                self._emit(
                    connection, "source.downloads_dropped", {"count": report.tickets.dropped}
                )
        report.stale = len(self._check_freshness())

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

    # -- bindings ----------------------------------------------------------------------------
    def _bindings(self) -> list[_Binding]:
        b, si, f = acquisition.source_binding, acquisition.source_item, acquisition.series_follow
        query = (
            sa.select(
                b.c.id, b.c.edition_id, b.c.work_id, b.c.active, b.c.disabled_reason,
                b.c.priority, b.c.last_checked, f.c.check_override, si.c.source_key,
                si.c.remote_id,
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
                    connection, "series.tombstoned_upstream",
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
    async def _discover_isolated(self, binding: _Binding, report: FollowReport) -> None:
        """One series failing is that series' problem: the others are still followed. An error
        that says the whole service cannot be driven is the exception: it goes up and pauses."""
        try:
            await self._discover(binding, report)
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

    async def _discover(self, binding: _Binding, report: FollowReport) -> None:
        ref = SeriesRef(source_key=binding.source_key, remote_id=binding.remote_id, title="")
        units = await from_service(self._service.list_units(self._fetcher, ref))
        # The first look at a binding only learns what is there: what to request from that
        # backlog is the watermark and backfill policy (L19), never "everything".
        baseline = binding.last_checked is None
        now = self._clock.now()
        opened: set[str] = set()

        def open_ticket(connection: sa.Connection, result: IngestResult) -> None:
            """In the ingest's own transaction: a Variant never exists without its ticket."""
            if result.outcome is not IngestOutcome.NEW_VARIANT or baseline:
                return
            DownloadTickets.open(
                connection, binding_id=binding.id, variant_id=result.variant_id,
                unit_id=result.unit_id, now=now,
            )  # fmt: skip
            opened.add(result.variant_id)

        for info in units:
            result = ingest_isolated(
                self._engine,
                IncomingUnit(
                    binding=BindingRef(binding.id, binding.edition_id),
                    remote_unit_id=info.remote_unit_id, raw_number=info.number, seen_at=now,
                    source_order=info.source_order, team_label=info.team, lang=info.lang,
                    title=info.title, published_at=info.published_at, kind=info.kind,
                ),
                then=open_ticket,
            )  # fmt: skip
            report.discovered += int(result.outcome is IngestOutcome.NEW_VARIANT)
        report.requested += len(opened)
        with self._engine.begin() as connection:
            table = acquisition.source_binding
            connection.execute(
                sa.update(table).where(table.c.id == binding.id).values(last_checked=now)
            )

    # -- freshness (EF-SRC-026 c) ------------------------------------------------------------
    def check_freshness(self) -> list[str]:
        """Editions newly stale (announced once per episode, `series.stale`)."""
        try:
            return self._check_freshness()
        finally:
            self._flush()

    def _check_freshness(self) -> list[str]:
        b, si = acquisition.source_binding, acquisition.source_item
        f, w = acquisition.series_follow, catalog.work
        query = (
            sa.select(b.c.edition_id)
            .select_from(
                b.join(si, si.c.id == b.c.source_item_id)
                .join(f, f.c.edition_id == b.c.edition_id)
                .join(w, w.c.id == b.c.work_id)
            )
            .where(
                si.c.plugin_id == self._plugin_id,
                b.c.active,
                sa.or_(f.c.check_override.is_(None), f.c.check_override.notin_(NOT_CHECKED)),
                w.c.status.notin_(FINISHED_STATUSES),
            )
            .distinct()
        )  # fmt: skip
        now = self._clock.now()
        announced: list[str] = []
        with self._engine.begin() as connection:
            for (edition_id,) in connection.execute(query).all():
                seen = self._discoveries(connection, edition_id)
                if not is_stale(seen, now, self._k) or self._already_announced(
                    connection, edition_id, max(seen)
                ):
                    continue
                self._emit(connection, "series.stale", {"edition_id": edition_id})
                announced.append(str(edition_id))
        return announced

    @staticmethod
    def _discoveries(connection: sa.Connection, edition_id: str) -> list[datetime]:
        v, u = catalog.unit_variant, catalog.unit
        return list(
            connection.execute(
                sa.select(v.c.first_seen_at)
                .select_from(v.join(u, u.c.id == v.c.unit_id))
                .where(u.c.edition_id == edition_id, v.c.removed_at.is_(None))
            ).scalars()
        )

    @staticmethod
    def _already_announced(connection: sa.Connection, edition_id: str, since: datetime) -> bool:
        event = platform.event
        return (
            connection.execute(
                sa.select(event.c.id).where(
                    event.c.type == "series.stale",
                    sa.func.json_extract(event.c.payload, "$.edition_id") == edition_id,
                    event.c.occurred_at >= since,
                )
            ).first()
            is not None
        )

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


def _probe_error(detail: str | None) -> SourceError:
    if detail == "source.auth_mode_unsupported":
        return AuthModeUnsupported()
    return ServiceVersionMismatch(detail or "the service failed its probe")


def follow_job(make_cycle: Callable[[], FollowCycle]) -> Callable[[JobContext], None]:
    """The Huey handler of the periodic `follow.cycle` (registered by the composition root)."""

    def handler(context: JobContext) -> None:
        asyncio.run(make_cycle().run())

    return handler
