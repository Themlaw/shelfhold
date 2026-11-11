"""Download tickets: ask the connected service, wait for "downloaded", receive, audit, file.

A `download_request` is the persistent ticket of one chapter Shelfhold asked for; only those are
ever retried (EF-SRC-026 b). The service is asked at a polite pace per upstream source
(`limit_key`), then polled. A chapter that is not "downloaded" yet is simply waited for; an
archive cut short, or a chapter that vanished from the server's queue, is retried; none of that is
a source failure (EF-SRC-028). Only `ContentRemoved` is counted, and an error that says the
service itself cannot be driven (authentication, version, schema) is not handled here: it goes up
to the cycle, which pauses the whole service.
"""

import hashlib
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.acquisition.application.failures import FailureLedger, from_service
from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.content_trust import UntrustedEntryError
from shelfhold.infrastructure.fetcher import FetchError, FetchTooLarge, GuardedFetcher
from shelfhold.infrastructure.fetcher.errors import InvalidFetchUrl
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import untrusted_name
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.jobs.public import DomainSlots
from shelfhold.library.public import (
    PassthroughStore,
    Received,
    VariantAlreadyStoredError,
    absorb_echo,
    receive_cbz,
    sniff_kind,
)
from shelfhold.sdk.connected import ConnectedService, Ticket
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.refs import FileAsset, SeriesRef, UnitInfo, UnitRef
from shelfhold.sdk.source import RateLimited, SourceChanged, TemporarilyUnavailable

MAX_ATTEMPTS = 8
log = structlog.get_logger(__name__)

STALL_AFTER = timedelta(hours=6)  # EF-SRC-026 b: a chapter still not downloaded after N hours
MAX_REQUEUE = 20  # per cycle, in Units: the cap of source.downloads_dropped
MAX_WAIT_S = 120.0  # the most one cycle sleeps for the pace of the sources
CLOCK_GRAIN_S = 1e-3  # a wait shorter than this is over (clocks tick in microseconds)
JITTER = 0.5  # each gap is `interval x (1 + up to JITTER)`, so instances never march in step
OPEN_STATES = ("requested", "downloaded")
Announce = Callable[[Connection, str, dict[str, object]], None]


def jitter_fraction(request_id: str) -> float:
    """A stable value in [0, JITTER) for a request (EF-SRC-009): no random source in tests."""
    digest = hashlib.sha256(request_id.encode()).digest()
    return JITTER * int.from_bytes(digest[:4], "big") / 2**32


def oriented_name(filename: str, staged: Path) -> str:
    """The name a received file is filed under: the service's stem, and the extension its bytes
    call for (`.epub` or `.cbz`), never the one the service gave it (EF-BIB-051)."""
    return f"{PurePath(filename).stem}{sniff_kind(staged)}"


class Receiver(Protocol):
    def __call__(self, asset: FileAsset) -> AbstractAsyncContextManager[Received]: ...


# (unit id, variant id, file name) -> local path in /library; the composition root decides.
Placement = Callable[[str, str, str], str]
Sleep = Callable[[float], Awaitable[None]]
Authorize = Callable[[str, Mapping[str, str]], tuple[str, dict[str, str]]]
QueueEmpty = Callable[[], Awaitable[bool]]  # the server's download queue holds nothing


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


class CycleAbandoned(Exception):
    """The cycle cannot write here now (a root away, a lease refused, a frozen instance): it
    stops before downloading anything, counts no failure against the source and spends no attempt
    of the ticket. The next cycle asks again."""


class LeasedReceiver:
    """A `Receiver` that first obtains what the filing needs (`acquire`: the lease on the library),
    and only then lets the bytes come: a download whose result cannot be filed is a download for
    nothing, and its failure would be counted against the chapter."""

    def __init__(self, inner: Receiver, acquire: Callable[[], object]) -> None:
        self._inner = inner
        self._acquire = acquire

    def __call__(self, asset: FileAsset) -> AbstractAsyncContextManager[Received]:
        return _Leased(self._inner(asset), self._acquire)


class _Leased:
    def __init__(
        self, inner: AbstractAsyncContextManager[Received], acquire: Callable[[], object]
    ) -> None:
        self._inner = inner
        self._acquire = acquire

    async def __aenter__(self) -> Received:
        try:
            self._acquire()
        except Exception as error:
            raise CycleAbandoned(type(error).__name__) from error
        return await self._inner.__aenter__()

    async def __aexit__(self, *exc: Any) -> bool | None:
        return await self._inner.__aexit__(*exc)


def _is_transport(error: BaseException) -> bool:
    """The server or the network, not the file: nothing to count and no attempt to spend."""
    if isinstance(error, FetchTooLarge):
        return False  # the same bytes will be too large again
    return isinstance(error, RateLimited | TemporarilyUnavailable | FetchError | OSError)


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass
class TicketReport:
    submitted: int = 0
    waiting: int = 0
    fetched: int = 0
    retried: int = 0
    abandoned: int = 0
    dropped: int = (
        0  # asked again after N hours, or given up: announced as source.downloads_dropped
    )
    deferred: int = 0  # held back by the pace or by a busy slot, tried again next cycle
    refused: int = 0  # archives with an entry off the allowlist: closed, announced, not counted
    echoed: int = 0  # our own export handed back: a replica, no Variant (I50)


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
    requested_at: datetime
    updated_at: datetime | None = None  # the last (re)submission: the clock of the next requeue


Blocked = Callable[[OpenRequest], bool]  # a tombstone covers the chapter or its series


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
        max_wait_s: float = MAX_WAIT_S,
        stall_after: timedelta = STALL_AFTER,
        announce: Announce | None = None,
        requeue_cap: int = MAX_REQUEUE,
        queue_empty: QueueEmpty | None = None,
        blocked: Blocked | None = None,
        filtered: Blocked | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._announce = announce
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
        self._max_wait_s = max_wait_s
        self._stall_after = stall_after
        self._requeue_cap = requeue_cap
        self._queue_empty = queue_empty  # None: the service cannot say, nothing is asked again
        self._blocked = blocked
        self._filtered = filtered  # a download filter refuses it now (EF-SRC-039)
        self._queue_checked: bool | None = None  # read once per cycle, when first needed
        self.unloaded: frozenset[str] = frozenset()  # sources the probe found unusable this cycle

    # -- opening -----------------------------------------------------------------------------
    @staticmethod
    def open(
        connection: Connection, *, binding_id: str, variant_id: str, unit_id: str, now: datetime
    ) -> bool:
        """One open ticket per Variant and per Unit (I52): a second call while one is open, for
        the same Variant or for another Variant of the same Unit, changes nothing."""
        request_id = new_ulid()
        inserted = connection.execute(
            sqlite_insert(acquisition.download_request)
            .values(
                id=request_id, target_kind="unit", variant_id=variant_id,
                source_binding_id=binding_id, state="requested", requested_at=now, unit_id=unit_id,
            )
            .on_conflict_do_nothing()  # the partial unique indexes: one open ticket per Variant
            # and per Unit, whatever the writers
        ).rowcount  # fmt: skip
        if not inserted:
            return False
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
                u.c.source_order, r.c.requested_at, r.c.updated_at,
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
            found = [OpenRequest(*tuple(row)) for row in connection.execute(query)]
        return [r for r in found if r.source_key not in self.unloaded]  # degraded: left alone

    # -- advancing ---------------------------------------------------------------------------
    async def advance(self) -> TicketReport:
        """Poll and fetch what was already asked; ask for the rest at the pace of each source.

        Sources are independent: a source that has to wait never holds back another, and the
        cycle only sleeps when every source with something to ask is waiting (and for no more than
        `max_wait_s` in all: what is left is asked next cycle).
        """
        report = TicketReport()
        self._queue_checked = None
        queues: dict[str, deque[OpenRequest]] = {}
        for request in self._open_requests():
            if request.ticket is None:
                if self._filtered is not None and self._filtered(request):
                    self._update(request.id, state="cancelled")  # never sent: reopened or not
                    continue
                queues.setdefault(request.source_key, deque()).append(request)
            else:
                await self._guarded(request, report)
        await self._drain(queues, report)
        log.debug(
            "follow.tickets_advanced",
            submitted=report.submitted,
            fetched=report.fetched,
            waiting=report.waiting,
            retried=report.retried,
            abandoned=report.abandoned,
            refused=report.refused,
            deferred=report.deferred,
        )
        return report

    async def _guarded(self, request: OpenRequest, report: TicketReport) -> None:
        try:
            await self._advance_one(request, report)
        except CycleAbandoned:
            raise  # nothing can be filed now: the cycle stops, nothing is counted
        except Exception as error:
            if disposition_of(classify(error)) is Disposition.PAUSE:
                raise  # the service cannot be driven: the cycle pauses it, nothing is counted
            self._failed(request, error, report)

    async def _drain(self, queues: dict[str, deque[OpenRequest]], report: TicketReport) -> None:
        with self._engine.connect() as connection:
            next_at = self._ledger.next_at(connection)
        waited = 0.0
        while queues:
            now = self._clock.now().timestamp()
            ready = [key for key in queues if next_at.get(key, 0.0) <= now + CLOCK_GRAIN_S]
            if not ready:
                wait = min(next_at[key] for key in queues) - now
                if waited + wait > self._max_wait_s:
                    report.deferred += sum(len(queue) for queue in queues.values())
                    return
                await self._sleep(wait)
                waited += wait
                continue
            for key in ready:
                request = queues[key].popleft()
                if not self._take_slot(key):
                    report.deferred += 1 + len(queues.pop(key))
                    continue
                try:
                    await self._guarded(request, report)
                finally:
                    self._release_slot(key)
                interval = 1.0 / self._service.manifest.rate_limit.requests_per_second
                next_at[key] = self._clock.now().timestamp() + interval * (
                    1.0 + jitter_fraction(request.id)
                )
                with self._engine.begin() as connection:
                    self._ledger.set_next_at(connection, key, next_at[key])
                if not queues[key]:
                    del queues[key]

    def _take_slot(self, key: str) -> bool:
        if self._slots is None:
            return True
        self._slots.declare(key, self._service.manifest.rate_limit.concurrency)
        return self._slots.try_acquire(key)

    def _release_slot(self, key: str) -> None:
        if self._slots is not None:
            self._slots.release(key)

    async def _advance_one(self, request: OpenRequest, report: TicketReport) -> None:
        if request.ticket is None:
            request = replace(request, ticket=await self._submit(request))
            report.submitted += 1
        ticket = Ticket(token=str(request.ticket), target_kind="unit")
        state = await from_service(self._service.poll(self._fetcher, ticket))
        log.debug(
            "follow.ticket_polled", request_id=request.id, unit_id=request.unit_id, state=state
        )
        if state in ("queued", "downloading"):
            await self._wait_or_requeue(request, report)  # not finished: wait, no failure
            return
        if state in ("failed", "gone"):  # dropped or renumbered on the server: ask again
            self._retry(request, report, new_ticket=True)
            return
        self._update(request.id, state="downloaded")
        await self._fetch(request, ticket, report)

    async def _submit(self, request: OpenRequest) -> str:
        target = UnitRef(
            series=SeriesRef(source_key=request.source_key, remote_id=request.remote_id, title=""),
            unit=UnitInfo(
                remote_unit_id=request.remote_unit_id,
                source_order=request.source_order,
                number=request.number,
            ),
        )
        ticket = await from_service(self._service.request(self._fetcher, target))
        self._update(request.id, ticket=ticket.token)  # the token itself is never logged
        log.debug("follow.ticket_requested", request_id=request.id, unit_id=request.unit_id)
        return ticket.token

    async def _fetch(self, request: OpenRequest, ticket: Ticket, report: TicketReport) -> None:
        payload = await from_service(self._service.fetch(self._fetcher, ticket))
        if not isinstance(payload, FileAsset):
            raise SourceChanged("a connected service must answer a file")
        try:
            receiving = self._receive(payload)
        except InvalidFetchUrl as error:  # the plugin pointed the credential at another origin
            raise SourceChanged("the service handed back a URL off its origin") from error
        async with receiving as received:
            # what the bytes are, not what the service called them (EF-BIB-051): an EPUB goes to
            # the trust sort and the neutralizer, whatever its file name says
            oriented = oriented_name(payload.filename, received.path)
            echo = absorb_echo(  # I50: our own export, handed back, is a replica and no Variant
                self._engine, request_id=request.id, staged=received.path,
                kind=sniff_kind(received.path), file_hash=received.sha256, size=received.size,
                remote_ref=request.remote_unit_id, keep_shell=True,
            )  # fmt: skip
            if echo is not None:
                report.echoed += 1
                return
            with suppress(VariantAlreadyStoredError):  # a replay after a crash: already there
                self._store.ingest(
                    unit_id=request.unit_id, variant_id=request.variant_id,
                    staged=received.path, expected_sha256=received.sha256,
                    dest=self._place(request.unit_id, request.variant_id, oriented),
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
        log.debug(
            "follow.chapter_fetched",
            request_id=request.id,
            unit_id=request.unit_id,
            variant_id=request.variant_id,
            size=received.size,
            filename=untrusted_name(payload.filename),
        )

    # -- failures ----------------------------------------------------------------------------
    def _failed(self, request: OpenRequest, error: Exception, report: TicketReport) -> None:
        log.warning(
            "follow.ticket_failed",
            request_id=request.id,
            failure_class=classify(error),
            error=type(error).__name__,
        )
        if disposition_of(classify(error)) is Disposition.REFUSE:
            self._refuse(request, error, report)
        elif disposition_of(classify(error)) is Disposition.COUNT:
            now = self._clock.now()
            with self._engine.begin() as connection:
                self._ledger.count_content(
                    connection, request.binding_id, request.source_key,
                    getattr(error, "code", "source.content_removed"), now,
                )  # fmt: skip
                self._ledger.record_download(connection, request.source_key, ok=False, now=now)
            self._update(request.id, state="abandoned")
            report.abandoned += 1
        elif _is_transport(error):
            report.waiting += 1  # asleep, busy, unreachable, a 5xx: not a failed attempt
        else:  # a truncated archive...: another attempt, nothing counted against the source
            self._retry(request, report)

    def _refuse(self, request: OpenRequest, error: Exception, report: TicketReport) -> None:
        """EF-BIB-051 (6): an entry off the allowlist. The same bytes would be refused again, so
        the ticket is closed for good (`abandoned` keeps the Unit counted as asked: neither the
        follow cycle nor a backfill asks again), the admin is told once, and nothing is counted
        against the source: it did not fail, and the Unit is not exported because it was never
        filed. The event holds the Unit and a closed reason, never an entry name (it is the
        archive's own text)."""
        reason = error.kind if isinstance(error, UntrustedEntryError) else "not_allowlisted"
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(acquisition.download_request)
                .where(acquisition.download_request.c.id == request.id)
                .values(state="abandoned", updated_at=self._clock.now())
            )
            if self._announce is not None:
                self._announce(
                    connection,
                    UntrustedEntryError.code,
                    {"unit_id": request.unit_id, "reason": reason},
                )
        report.refused += 1
        log.warning("follow.entry_refused", unit_id=request.unit_id, reason=reason)
        if isinstance(error, UntrustedEntryError) and error.entry:
            log.debug("follow.entry_refused_name", unit_id=request.unit_id,
                      entry=untrusted_name(error.entry))  # fmt: skip

    async def _wait_or_requeue(self, request: OpenRequest, report: TicketReport) -> None:
        """Wait for the server. Only past N hours is the chapter asked again (EF-SRC-026 b)."""
        if await self._may_requeue(request, report) and self._requeue(request, report):
            return
        report.waiting += 1

    async def _may_requeue(self, request: OpenRequest, report: TicketReport) -> bool:
        """The chapter is one Shelfhold asked for (this table holds nothing else) and has waited
        more than N hours since it was last sent; the cap of the cycle is not reached; no
        tombstone covers it; and the server's own queue is empty: a chapter still in the queue is
        being worked on, and a queue that holds another client's downloads is not ours to disturb.
        A source that is paused or unloaded never gets here: its requests are left out."""
        if report.dropped >= self._requeue_cap:
            return False
        since = max(_utc(request.requested_at), _utc(request.updated_at or request.requested_at))
        if self._clock.now() - since <= self._stall_after:
            return False
        if self._blocked is not None and self._blocked(request):
            return False
        return await self._server_queue_is_empty()

    async def _server_queue_is_empty(self) -> bool:
        if self._queue_empty is None:
            return False
        if self._queue_checked is None:
            try:
                self._queue_checked = await self._queue_empty()
            except Exception as error:
                if disposition_of(classify(error)) is Disposition.PAUSE:
                    raise  # the service cannot be driven: the cycle pauses it
                log.warning("follow.queue_unreadable", failure_class=classify(error))
                self._queue_checked = False  # unknown is not empty: nothing is asked again
        return self._queue_checked

    def _requeue(self, request: OpenRequest, report: TicketReport) -> bool:
        """Ask the chapter again: its ticket goes (the next cycle sends it once more), its clock
        starts again. One compare-and-set on the row it was read with, so a replay or a second
        worker that read the same row changes nothing and counts nothing (no double send)."""
        attempts = request.attempts + 1
        given_up = attempts >= self._max_attempts
        values: dict[str, object] = {"attempts": attempts, "updated_at": self._clock.now()}
        values.update({"state": "abandoned"} if given_up else {"ticket": None})
        table = acquisition.download_request
        with self._engine.begin() as connection:
            changed = connection.execute(
                sa.update(table)
                .where(
                    table.c.id == request.id,
                    table.c.state == "requested",
                    table.c.ticket == request.ticket,
                    table.c.attempts == request.attempts,
                )
                .values(**values)
            ).rowcount
            if changed and given_up:
                self._ledger.record_download(
                    connection, request.source_key, ok=False, now=self._clock.now()
                )
        if not changed:
            return False
        report.dropped += 1
        if given_up:
            report.abandoned += 1
        else:
            report.retried += 1
        return True

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
            report.dropped += 1
            return
        values: dict[str, object] = {"state": "requested", "attempts": attempts}
        if new_ticket:
            values["ticket"] = None
        self._update(request.id, **values)
        report.retried += 1

    def reopen_abandoned(self, source_keys: frozenset[str] | None = None) -> int:
        """The source is healthy again: what was given up after repeated attempts is asked once
        more (EF-SRC-026 b), for the upstream sources named (all of them by default). A chapter
        the server reported gone for good (`content`) stays closed.
        """
        r = acquisition.download_request
        other = sa.alias(r)
        b, si = acquisition.source_binding, acquisition.source_item
        only = (
            [
                r.c.source_binding_id.in_(
                    sa.select(b.c.id)
                    .join(si, si.c.id == b.c.source_item_id)
                    .where(si.c.source_key.in_(source_keys))
                )
            ]
            if source_keys is not None
            else []
        )
        with self._engine.begin() as connection:
            result = connection.execute(
                sa.update(r)
                .where(
                    r.c.state == "abandoned",
                    r.c.attempts >= self._max_attempts,
                    *only,
                    ~sa.exists().where(
                        sa.or_(
                            other.c.variant_id == r.c.variant_id,
                            other.c.unit_id == r.c.unit_id,  # the next rank holds the Unit (I52)
                        ),
                        other.c.state.in_(OPEN_STATES),
                    ),
                    ~sa.exists().where(  # two given up for one Unit: only the older comes back
                        other.c.unit_id == r.c.unit_id,
                        other.c.state == "abandoned",
                        other.c.attempts >= self._max_attempts,
                        other.c.id < r.c.id,
                    ),
                )
                .values(state="requested", attempts=0, ticket=None, updated_at=self._clock.now())
            )
        return int(result.rowcount)

    def _update(self, request_id: str, **values: object) -> None:
        table = acquisition.download_request
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(table)
                .where(table.c.id == request_id)
                .values(updated_at=self._clock.now(), **values)
            )
