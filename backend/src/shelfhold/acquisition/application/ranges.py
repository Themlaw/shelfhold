"""Requests for a range of Units: one ticket, one file, one `export_item` (EF-SRC-032, EF-SRC-034).

A source with `ranged_content` delivers one file for several Units (an EPUB per volume). The
ticket is one `download_request` row of kind `range`; the Units it covers are its
`download_request_unit` rows, and the per-cycle cap counts those. The answer becomes one
`export_item` whose coverage (`unit_coverage`) says which Units it carries, in order, with their
bounds in the file when the source knows them. A closed volume is never asked again; the rolling
one is asked again whole when a chapter joins it, and its item is updated in place.

Only Units that were asked and not covered are ever retried (EF-SRC-026 b): `retry_uncovered`.
The bytes themselves are the host store's business (`keep`): in M1a this lot fixes the contract
and the persistence of tickets and coverage, the real range source arrives in M2.
"""

import contextlib
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import groupby
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.failures import from_service
from shelfhold.acquisition.application.lncrawl_ranges import ticket_expired, verify_range_answer
from shelfhold.acquisition.application.volume_cut import is_cut
from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.acquisition.domain.requests import RequestBudget
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export
from shelfhold.library.public import Received, readable_unit_ids
from shelfhold.sdk.connected import ConnectedService, Ticket
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.refs import FileAsset, RangedPayload, RangeRef, SeriesRef, UnitInfo
from shelfhold.sdk.source import SourceChanged

OPEN = ("requested", "downloaded")
MAX_ATTEMPTS = 8

RangeReceiver = Callable[[FileAsset], AbstractAsyncContextManager[Received]]
# (received file, payload, export_item id): the host keeps the bytes as that item
Keeper = Callable[[Received, RangedPayload, str], None]


ABANDON_DELAY = timedelta(days=1)  # first wait before an abandoned range is asked again
ABANDON_CAP = timedelta(days=30)


@dataclass(frozen=True)
class RangeUnit:
    unit_id: str
    info: UnitInfo


@dataclass
class RangeRequest:
    ticket_ids: list[str] = field(default_factory=list)
    deferred: list[RangeUnit] = field(default_factory=list)  # what the caps leave to the next cycle


@dataclass
class RangeReport:
    submitted: int = 0
    waiting: int = 0
    fetched: int = 0
    retried: int = 0
    abandoned: int = 0
    expired: int = 0  # tickets let go after waiting longer than the bound


class RangeTickets:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        service: ConnectedService,
        fetcher: Fetcher,
        receive: RangeReceiver,
        keep: Keeper,
        *,
        edition_id: str,
        binding_id: str,
        series: SeriesRef,
        origin: str | None = None,
    ) -> None:
        self._origin = origin  # the origin a file of this service may come from (EF-SRC-034)
        self._engine = engine
        self._clock = clock
        self._service = service
        self._fetcher = fetcher
        self._receive = receive
        self._keep = keep
        self._edition_id = edition_id
        self._binding_id = binding_id
        self._series = series

    # -- planning ----------------------------------------------------------------------------
    def plan(self, units: Sequence[RangeUnit]) -> list[list[RangeUnit]]:
        """The ranges worth asking for, per volume label, whole.

        Left out of a range: the Units already asked, "stop following" ones, those a file of their
        own already carries, and those a *closed* item covers. Units of the volume still rolling
        stay in, so that the request covers the whole volume and its item is updated in place:
        two items never share a Unit. A range with nothing new in it is not asked.
        """
        ordered = sorted(
            (u for u in units if u.info.volume_label), key=lambda u: u.info.source_order
        )
        with self._engine.connect() as connection:
            closed, rolling = self._covered_by(connection)
            asked = self._asked(connection)
            given_up = self._given_up(connection, self._clock.now())
            readable = readable_unit_ids(connection, self._edition_id)
            suppressed = set(
                connection.execute(
                    sa.select(catalog.unit.c.id).where(
                        catalog.unit.c.edition_id == self._edition_id,
                        catalog.unit.c.follow_suppressed,
                    )
                ).scalars()
            )
        plan: list[list[RangeUnit]] = []
        for _label, run in groupby(ordered, key=lambda u: u.info.volume_label):
            group = [
                u
                for u in run
                if u.unit_id not in asked
                and u.unit_id not in closed
                and u.unit_id not in suppressed
                and (u.unit_id in rolling or u.unit_id not in readable)
            ]
            # a range given up (a queue that never ends, a refused file) waits, whatever joins it,
            # for a delay that doubles with each abandon: a service that fails is not hammered
            if any(u.unit_id in given_up for u in group):
                continue
            if any(u.unit_id not in rolling for u in group):
                plan.append(group)
        return plan

    def _covered_by(self, connection: Connection) -> tuple[set[str], set[str]]:
        """(Units covered by a closed item, Units covered by an item still rolling)."""
        coverage, item = export.unit_coverage, export.export_item
        closed: set[str] = set()
        rolling: set[str] = set()
        for row in connection.execute(
            sa.select(coverage.c.unit_id, item.c.closed)
            .select_from(
                coverage.join(
                    item,
                    sa.and_(
                        coverage.c.carrier_type == "export_item", coverage.c.carrier_id == item.c.id
                    ),
                )
            )
            .where(item.c.edition_id == self._edition_id)
        ):
            (closed if row.closed else rolling).add(row.unit_id)
        return closed, rolling

    def _covered(self, connection: Connection) -> set[str]:
        coverage = export.unit_coverage
        return set(
            connection.execute(
                sa.select(coverage.c.unit_id).where(coverage.c.carrier_type == "export_item")
            ).scalars()
        )

    def _asked(self, connection: Connection) -> set[str]:
        r, ru = acquisition.download_request, acquisition.download_request_unit
        return set(
            connection.execute(
                sa.select(ru.c.unit_id)
                .join(r, r.c.id == ru.c.download_request_id)
                .where(r.c.state.in_(OPEN), ru.c.state == "requested")
            ).scalars()
        )

    def _given_up(self, connection: Connection, now: datetime) -> set[str]:
        """Units whose range was abandoned and whose delay has not run out (a day, doubled by
        each abandon of that Unit, at most a month)."""
        r, ru = acquisition.download_request, acquisition.download_request_unit
        abandons: dict[str, list[datetime]] = {}
        for row in connection.execute(
            sa.select(ru.c.unit_id, r.c.updated_at)
            .join(r, r.c.id == ru.c.download_request_id)
            .where(r.c.state == "abandoned", ru.c.state == "requested")
        ):
            abandons.setdefault(row.unit_id, []).append(row.updated_at)
        return {
            unit
            for unit, stamps in abandons.items()
            if now - max(stamps) < min(ABANDON_DELAY * 2 ** (len(stamps) - 1), ABANDON_CAP)
        }

    # -- requesting --------------------------------------------------------------------------
    def _grant(
        self, units: Sequence[RangeUnit], budget: RequestBudget
    ) -> tuple[list[RangeUnit], list[RangeUnit]]:
        """A range is asked whole or not at all this cycle. Only a range larger than any cycle
        could ever grant is cut at the cap (EF-SRC-034); the rest follows next cycle."""
        key = self._series.source_key
        if units and all(is_cut(u.info) for u in units):
            # A volume that Shelfhold cut is one file, one `export_item` (never split at the cap;
            # its size is bounded by the cap already, see `cut_volumes`). The key is shared by
            # every series of the plugin: a volume that does not fit keeps the key free for the
            # next cycle, where it goes before anything else (no starvation).
            if len(units) > budget.room(key, claiming=True) or len(units) > budget.capacity:
                budget.hold(key, len(units))
                return [], list(units)
            budget.take(key, len(units), claiming=True)
            return list(units), []
        if not units or (len(units) > budget.room(key) and len(units) <= budget.capacity):
            return [], list(units)
        granted = budget.take(key, len(units))
        return list(units[:granted]), list(units[granted:])

    def request(self, units: Sequence[RangeUnit], budget: RequestBudget) -> RangeRequest:
        """One ticket for the range if the caps leave room for all of it (counted in Units);
        otherwise it waits whole, handed back."""
        chosen, deferred = self._grant(units, budget)
        result = RangeRequest(deferred=deferred)
        if chosen:
            with self._engine.begin() as connection:
                result.ticket_ids.append(self._open(connection, chosen))
        return result

    def _open(self, connection: Connection, units: Sequence[RangeUnit]) -> str:
        request_id = new_ulid()
        label = units[0].info.volume_label
        connection.execute(
            sa.insert(acquisition.download_request).values(
                id=request_id, target_kind="range", variant_id=None,
                source_binding_id=self._binding_id, state="requested",
                requested_at=self._clock.now(),
                range_ref={
                    "edition_id": self._edition_id, "volume_label": label,
                    "units": [
                        {"unit_id": u.unit_id, "info": u.info.model_dump(mode="json")}
                        for u in units
                    ],
                },
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(acquisition.download_request_unit),
            [
                {"download_request_id": request_id, "unit_id": u.unit_id, "state": "requested"}
                for u in units
            ],
        )
        return request_id

    # -- advancing ---------------------------------------------------------------------------
    async def advance(self) -> RangeReport:
        report = RangeReport()
        r = acquisition.download_request
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    r.c.id, r.c.ticket, r.c.attempts, r.c.range_ref, r.c.requested_at,
                    r.c.updated_at,
                )
                .where(
                    r.c.target_kind == "range", r.c.state.in_(OPEN),
                    r.c.source_binding_id == self._binding_id,
                )
                .order_by(r.c.requested_at, r.c.id)
            ).all()  # fmt: skip
        for row in rows:
            try:
                since = row.updated_at or row.requested_at
                await self._advance_one(row.id, row.ticket, row.range_ref, report, since)
            except Exception as error:
                failure_class = classify(error)
                if disposition_of(failure_class) is Disposition.PAUSE:
                    raise  # the service cannot be driven: the cycle pauses it
                self._failed(row.id, row.attempts, error, report)
        return report

    async def _advance_one(
        self,
        request_id: str,
        token: str | None,
        range_ref: dict[str, Any],
        report: RangeReport,
        since: datetime,
    ) -> None:
        units = [
            RangeUnit(u["unit_id"], UnitInfo.model_validate(u["info"])) for u in range_ref["units"]
        ]
        token_was_held = token is not None
        if token is None:
            target = RangeRef(
                series=self._series,
                first=units[0].info,
                last=units[-1].info,
                volume_label=range_ref.get("volume_label"),
            )
            token = (await from_service(self._service.request(self._fetcher, target))).token
            self._update(request_id, ticket=token)
            report.submitted += 1
        ticket = Ticket(token=token, target_kind="range")
        if token_was_held and ticket_expired(self._clock.now(), since):
            # a queue that never ends: the instance is told to drop it and the range is asked
            # again, a bounded number of times (`MAX_ATTEMPTS`)
            with contextlib.suppress(Exception):
                await self._service.cancel(self._fetcher, ticket)
            report.expired += 1
            self._retry(request_id, None, report)
            return
        state = await from_service(self._service.poll(self._fetcher, ticket))
        if state in ("queued", "downloading"):
            report.waiting += 1
            return
        if state in ("failed", "gone"):
            self._retry(request_id, None, report)
            return
        payload = await from_service(self._service.fetch(self._fetcher, ticket))
        if not isinstance(payload, RangedPayload):
            raise SourceChanged("a ranged ticket must be answered with a ranged payload")
        verify_range_answer(units, payload, origin=self._origin)  # before a byte is received
        async with self._receive(payload.asset) as received:
            with self._engine.begin() as connection:
                item_id = self._record(connection, request_id, units, payload)
                self._keep(received, payload, item_id)  # a refusal rolls the whole record back
        report.fetched += 1

    def _record(
        self,
        connection: Connection,
        request_id: str,
        units: Sequence[RangeUnit],
        payload: RangedPayload,
    ) -> str:
        v = catalog.unit_variant
        by_remote = {
            row.remote_unit_id: row.unit_id
            for row in connection.execute(
                sa.select(v.c.remote_unit_id, v.c.unit_id).where(
                    v.c.source_binding_id == self._binding_id,
                    v.c.remote_unit_id.in_(payload.covers),
                )
            )
        }
        taken = self._covered_by(connection)[0]  # a closed item keeps its Units: never two items
        covered = [
            (remote, by_remote[remote])
            for remote in payload.covers
            if remote in by_remote and by_remote[remote] not in taken
        ]
        item_id = self._rolling_item(connection, [unit for _, unit in covered])
        label = units[0].info.volume_label
        # a volume the cut closed is closed whatever the source says: it knows no volumes
        closed = payload.volume_closed or all(
            is_cut(u.info) and bool(u.info.volume_closed) for u in units
        )
        if item_id is None:
            item_id = new_ulid()
            connection.execute(
                sa.insert(export.export_item).values(
                    id=item_id, edition_id=self._edition_id, kind="volume" if label else "bundle",
                    frozen_attrs={"volume_label": label} if label else {},
                    closed=closed,
                )
            )  # fmt: skip
        else:  # the rolling volume grew: same item, new coverage
            coverage = export.unit_coverage
            connection.execute(
                sa.delete(coverage).where(
                    coverage.c.carrier_type == "export_item", coverage.c.carrier_id == item_id
                )
            )
            connection.execute(
                sa.update(export.export_item)
                .where(export.export_item.c.id == item_id)
                .values(closed=closed)
            )
        for position, (remote, unit_id) in enumerate(covered):
            start, end = payload.bounds.get(remote, (None, None))
            connection.execute(
                sa.insert(export.unit_coverage).values(
                    carrier_type="export_item", carrier_id=item_id, unit_id=unit_id,
                    position=position, extent="full", origin="provider",
                    start_locator=start, end_locator=end,
                )
            )  # fmt: skip
        ru = acquisition.download_request_unit
        got = {unit_id for _, unit_id in covered}
        for unit in units:
            connection.execute(
                sa.update(ru)
                .where(ru.c.download_request_id == request_id, ru.c.unit_id == unit.unit_id)
                .values(state="covered" if unit.unit_id in got else "missing")
            )
        connection.execute(
            sa.update(acquisition.download_request)
            .where(acquisition.download_request.c.id == request_id)
            .values(state="fetched", updated_at=self._clock.now())
        )
        return item_id

    def _rolling_item(self, connection: Connection, unit_ids: Sequence[str]) -> str | None:
        """An item of this Edition, not closed, that already covers some of these Units."""
        item, coverage = export.export_item, export.unit_coverage
        return connection.execute(
            sa.select(item.c.id)
            .select_from(
                item.join(
                    coverage,
                    sa.and_(
                        coverage.c.carrier_type == "export_item", coverage.c.carrier_id == item.c.id
                    ),
                )
            )
            .where(
                item.c.edition_id == self._edition_id,
                sa.not_(item.c.closed),
                coverage.c.unit_id.in_(unit_ids),
            )
            .order_by(item.c.created_at, item.c.id)
            .limit(1)
        ).scalar()

    # -- failures and retries ----------------------------------------------------------------
    def _failed(
        self, request_id: str, attempts: int, error: Exception, report: RangeReport
    ) -> None:
        if disposition_of(classify(error)) in (Disposition.COUNT, Disposition.REFUSE):
            self._update(request_id, state="abandoned")  # a refused archive is never asked again
            report.abandoned += 1
        else:
            self._retry(request_id, attempts, report)

    def _retry(self, request_id: str, attempts: int | None, report: RangeReport) -> None:
        r = acquisition.download_request
        with self._engine.begin() as connection:
            current = attempts
            if current is None:
                current = connection.execute(
                    sa.select(r.c.attempts).where(r.c.id == request_id)
                ).scalar_one()
        if current + 1 >= MAX_ATTEMPTS:
            self._update(request_id, state="abandoned", attempts=current + 1)
            report.abandoned += 1
        else:
            self._update(request_id, state="requested", attempts=current + 1, ticket=None)
            report.retried += 1

    def retry_uncovered(self, request_id: str, budget: RequestBudget) -> str | None:
        """A new ticket for the Units of this one that were asked and not covered; nothing else
        is asked again. None when nothing is left to ask, or it is already asked anew."""
        r, ru = acquisition.download_request, acquisition.download_request_unit
        with self._engine.begin() as connection:
            range_ref = connection.execute(
                sa.select(r.c.range_ref).where(r.c.id == request_id)
            ).scalar_one()
            missing = (
                {
                    row.unit_id
                    for row in connection.execute(
                        sa.select(ru.c.unit_id).where(
                            ru.c.download_request_id == request_id, ru.c.state == "missing"
                        )
                    )
                }
                - self._asked(connection)
                - self._covered(connection)
            )
            units = [
                RangeUnit(u["unit_id"], UnitInfo.model_validate(u["info"]))
                for u in range_ref["units"]
                if u["unit_id"] in missing
            ]
            chosen, _later = self._grant(units, budget)  # the cap counts a retry too
            return self._open(connection, chosen) if chosen else None

    def _update(self, request_id: str, **values: object) -> None:
        table = acquisition.download_request
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(table)
                .where(table.c.id == request_id)
                .values(updated_at=self._clock.now(), **values)
            )
