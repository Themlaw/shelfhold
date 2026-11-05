"""Download tickets: ask the connected service, wait for "downloaded", receive, audit, file.

A `download_request` is the persistent ticket of one chapter Shelfhold asked for; only those are
ever retried (EF-SRC-026 b). The service is asked at a polite pace per upstream source
(`limit_key`), then polled. A chapter that is not "downloaded" yet is simply waited for; an
archive cut short, or a chapter that vanished from the server's queue, is retried; none of that is
a source failure (EF-SRC-028). Only `ContentRemoved` is counted, and an error that says the
service itself cannot be driven (authentication, version, schema) is not handled here: it goes up
to the cycle, which pauses the whole service.
"""

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.failures import FailureLedger
from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fetcher import GuardedFetcher
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.jobs.public import DomainSlots
from shelfhold.library.public import (
    PassthroughStore,
    Received,
    VariantAlreadyStoredError,
    receive_cbz,
)
from shelfhold.sdk.connected import ConnectedService, Ticket
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.refs import FileAsset, SeriesRef, UnitInfo, UnitRef
from shelfhold.sdk.source import RateLimited, SourceChanged, TemporarilyUnavailable

MAX_ATTEMPTS = 8
OPEN_STATES = ("requested", "downloaded")


class Receiver(Protocol):
    def __call__(self, asset: FileAsset) -> AbstractAsyncContextManager[Received]: ...


# (unit id, variant id, file name) -> local path in /library; the composition root decides.
Placement = Callable[[str, str, str], str]
Sleep = Callable[[float], Awaitable[None]]
Authorize = Callable[[str, Mapping[str, str]], tuple[str, dict[str, str]]]


def bounded_receiver(
    fetcher: GuardedFetcher,
    *,
    staging_dir: Path,
    max_bytes: int,
    authorize: Authorize | None = None,
) -> Receiver:
    """Stream the archive into staging, counted and hashed, never past `max_bytes` (ENF-SEC-002).

    `authorize` adds the service credential (`AuthInjectingFetcher.authorize`); without it the
    request goes out as it is.
    """

    def receive(asset: FileAsset) -> AbstractAsyncContextManager[Received]:
        url, headers = asset.request.url, dict(asset.request.headers)
        if authorize is not None:
            url, headers = authorize(url, headers)
        return receive_cbz(
            fetcher, url, staging_dir=staging_dir, max_bytes=max_bytes, headers=headers
        )

    return receive


@dataclass
class TicketReport:
    submitted: int = 0
    waiting: int = 0
    fetched: int = 0
    retried: int = 0
    abandoned: int = 0
    deferred: int = 0  # held back by the pace or by a busy slot, tried again next cycle


@dataclass(frozen=True)
class OpenRequest:
    id: str
    binding_id: str
    variant_id: str
    unit_id: str
    remote_unit_id: str
    ticket: str | None
    attempts: int
    source_key: str
    remote_id: str
    number: str
    source_order: int


class DownloadTickets:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        service: ConnectedService,
        fetcher: Fetcher,
        ledger: FailureLedger,
        store: PassthroughStore,
        receive: Receiver,
        place: Placement,
        *,
        plugin_id: str,
        sleep: Sleep,
        slots: DomainSlots | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._service = service
        self._fetcher = fetcher
        self._ledger = ledger
        self._store = store
        self._receive = receive
        self._place = place
        self._plugin_id = plugin_id
        self._sleep = sleep
        self._slots = slots
        self._max_attempts = max_attempts
        self._next_at: dict[str, float] = {}  # limit_key -> earliest next request (monotonic s)

    # -- opening -----------------------------------------------------------------------------
    @staticmethod
    def open(connection: Connection, *, binding_id: str, variant_id: str, unit_id: str) -> bool:
        """One open ticket per Variant: a second call while one is open changes nothing."""
        table = acquisition.download_request
        if connection.execute(
            sa.select(table.c.id).where(
                table.c.variant_id == variant_id, table.c.state.in_(OPEN_STATES)
            )
        ).first():
            return False
        request_id = new_ulid()
        connection.execute(
            sa.insert(table).values(
                id=request_id, target_kind="unit", variant_id=variant_id,
                source_binding_id=binding_id, state="requested",
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(acquisition.download_request_unit).values(
                download_request_id=request_id, unit_id=unit_id, state="requested"
            )
        )
        return True

    def _open_requests(self) -> list[OpenRequest]:
        r, v, u = acquisition.download_request, catalog.unit_variant, catalog.unit
        b, si = acquisition.source_binding, acquisition.source_item
        query = (
            sa.select(
                r.c.id, r.c.source_binding_id, r.c.variant_id, v.c.unit_id, v.c.remote_unit_id,
                r.c.ticket, r.c.attempts, si.c.source_key, si.c.remote_id, u.c.number_raw,
                u.c.source_order,
            )
            .select_from(
                r.join(v, v.c.id == r.c.variant_id)
                .join(u, u.c.id == v.c.unit_id)
                .join(b, b.c.id == r.c.source_binding_id)
                .join(si, si.c.id == b.c.source_item_id)
            )
            .where(
                r.c.state.in_(OPEN_STATES), r.c.target_kind == "unit", b.c.active,
                si.c.plugin_id == self._plugin_id,
            )
            .order_by(r.c.requested_at, r.c.id)
        )  # fmt: skip
        with self._engine.connect() as connection:
            return [OpenRequest(*row) for row in connection.execute(query)]

    # -- advancing ---------------------------------------------------------------------------
    async def advance(self) -> TicketReport:
        report = TicketReport()
        for request in self._open_requests():
            try:
                await self._advance_one(request, report)
            except Exception as error:
                if disposition_of(classify(error)) is Disposition.PAUSE:
                    raise  # the service cannot be driven: the cycle pauses it, nothing is counted
                self._failed(request, error, report)
        return report

    async def _advance_one(self, request: OpenRequest, report: TicketReport) -> None:
        if request.ticket is None:
            token = await self._submit(request)
            if token is None:
                report.deferred += 1
                return
            request = replace(request, ticket=token)
            report.submitted += 1
        ticket = Ticket(token=str(request.ticket), target_kind="unit")
        state = await self._service.poll(self._fetcher, ticket)
        if state in ("queued", "downloading"):
            report.waiting += 1  # Suwayomi has not finished: wait, no failure
            return
        if state in ("failed", "gone"):  # dropped or renumbered on the server: ask again
            self._retry(request, report, new_ticket=True)
            return
        self._update(request.id, state="downloaded")
        await self._fetch(request, ticket, report)

    async def _submit(self, request: OpenRequest) -> str | None:
        key = request.source_key
        if self._slots is not None:
            self._slots.declare(key, self._service.manifest.rate_limit.concurrency)
            if not self._slots.try_acquire(key):
                return None
        try:
            await self._pace(key)
            target = UnitRef(
                series=SeriesRef(source_key=key, remote_id=request.remote_id, title=""),
                unit=UnitInfo(
                    remote_unit_id=request.remote_unit_id,
                    source_order=request.source_order,
                    number=request.number,
                ),
            )
            ticket = await self._service.request(self._fetcher, target)
        finally:
            if self._slots is not None:
                self._slots.release(key)
        self._update(request.id, ticket=ticket.token)
        return ticket.token

    async def _pace(self, limit_key: str) -> None:
        """At most `requests_per_second` requests per upstream source; another source is free."""
        interval = 1.0 / self._service.manifest.rate_limit.requests_per_second
        now = self._clock.now().timestamp()
        start = max(now, self._next_at.get(limit_key, now))
        if start > now:
            await self._sleep(start - now)
        self._next_at[limit_key] = start + interval

    async def _fetch(self, request: OpenRequest, ticket: Ticket, report: TicketReport) -> None:
        payload = await self._service.fetch(self._fetcher, ticket)
        if not isinstance(payload, FileAsset):
            raise SourceChanged("a connected service must answer a file")
        async with self._receive(payload) as received:
            with suppress(VariantAlreadyStoredError):  # a replay after a crash: already there
                self._store.ingest(
                    unit_id=request.unit_id, variant_id=request.variant_id,
                    staged=received.path, expected_sha256=received.sha256,
                    dest=self._place(request.unit_id, request.variant_id, payload.filename),
                    upstream_ref=request.remote_unit_id,
                )  # fmt: skip
        now = self._clock.now()
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(acquisition.download_request)
                .where(acquisition.download_request.c.id == request.id)
                .values(state="fetched", updated_at=now)
            )
            connection.execute(
                sa.update(acquisition.download_request_unit)
                .where(acquisition.download_request_unit.c.download_request_id == request.id)
                .values(state="covered")
            )
            self._ledger.reset_content(connection, request.binding_id, request.source_key)
            self._ledger.record_download(connection, request.source_key, ok=True, now=now)
        report.fetched += 1

    # -- failures ----------------------------------------------------------------------------
    def _failed(self, request: OpenRequest, error: Exception, report: TicketReport) -> None:
        if disposition_of(classify(error)) is Disposition.COUNT:
            now = self._clock.now()
            with self._engine.begin() as connection:
                self._ledger.count_content(
                    connection, request.binding_id, request.source_key,
                    getattr(error, "code", "source.content_removed"), now,
                )  # fmt: skip
                self._ledger.record_download(connection, request.source_key, ok=False, now=now)
            self._update(request.id, state="abandoned")
            report.abandoned += 1
        elif isinstance(error, RateLimited | TemporarilyUnavailable):
            report.waiting += 1  # the server is asleep or busy: not a failed attempt
        else:  # truncated archive, reset connection...: another attempt, nothing counted
            self._retry(request, report)

    def _retry(
        self, request: OpenRequest, report: TicketReport, *, new_ticket: bool = False
    ) -> None:
        attempts = request.attempts + 1
        if attempts >= self._max_attempts:
            self._update(request.id, state="abandoned", attempts=attempts)
            with self._engine.begin() as connection:
                self._ledger.record_download(
                    connection, request.source_key, ok=False, now=self._clock.now()
                )
            report.abandoned += 1
            return
        values: dict[str, object] = {"state": "requested", "attempts": attempts}
        if new_ticket:
            values["ticket"] = None
        self._update(request.id, **values)
        report.retried += 1

    def _update(self, request_id: str, **values: object) -> None:
        table = acquisition.download_request
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(table)
                .where(table.c.id == request_id)
                .values(updated_at=self._clock.now(), **values)
            )
