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

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from itertools import groupby
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.failures import from_service
from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.acquisition.domain.requests import RequestBudget
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export
from shelfhold.library.public import Received
from shelfhold.sdk.connected import ConnectedService, Ticket
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.refs import FileAsset, RangedPayload, RangeRef, SeriesRef, UnitInfo
from shelfhold.sdk.source import SourceChanged

OPEN = ("requested", "downloaded")
MAX_ATTEMPTS = 8

RangeReceiver = Callable[[FileAsset], AbstractAsyncContextManager[Received]]
# (received file, payload, export_item id): the host keeps the bytes as that item
Keeper = Callable[[Received, RangedPayload, str], None]


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
    ) -> None:
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
        """The ranges worth asking for: per volume label, whole, when some Unit of it is neither
        covered by a file nor already in an open ticket. A closed volume that is fully covered is
        never asked again; the rolling one is, when a chapter joined it."""
        ordered = sorted(
            (u for u in units if u.info.volume_label), key=lambda u: u.info.source_order
        )
        with self._engine.connect() as connection:
            covered = self._covered(connection)
            asked = self._asked(connection)
        plan: list[list[RangeUnit]] = []
        for _label, run in groupby(ordered, key=lambda u: u.info.volume_label):
            group = list(run)
            if any(u.unit_id not in covered and u.unit_id not in asked for u in group):
                plan.append(group)
        return plan

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

    # -- requesting --------------------------------------------------------------------------
    def request(self, units: Sequence[RangeUnit], budget: RequestBudget) -> RangeRequest:
        """One ticket for as many of the Units as the caps allow (counted in Units); the rest is
        handed back, to be asked in a later cycle."""
        granted = budget.take(self._series.source_key, len(units)) if units else 0
        chosen, deferred = list(units[:granted]), list(units[granted:])
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
                sa.select(r.c.id, r.c.ticket, r.c.attempts, r.c.range_ref)
                .where(
                    r.c.target_kind == "range", r.c.state.in_(OPEN),
                    r.c.source_binding_id == self._binding_id,
                )
                .order_by(r.c.requested_at, r.c.id)
            ).all()  # fmt: skip
        for row in rows:
            try:
                await self._advance_one(row.id, row.ticket, row.range_ref, report)
            except Exception as error:
                failure_class = classify(error)
                if disposition_of(failure_class) is Disposition.PAUSE:
                    raise  # the service cannot be driven: the cycle pauses it
                self._failed(row.id, row.attempts, error, report)
        return report

    async def _advance_one(
        self, request_id: str, token: str | None, range_ref: dict[str, Any], report: RangeReport
    ) -> None:
        units = [
            RangeUnit(u["unit_id"], UnitInfo.model_validate(u["info"])) for u in range_ref["units"]
        ]
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
        covered = [(remote, by_remote[remote]) for remote in payload.covers if remote in by_remote]
        item_id = self._rolling_item(connection, [unit for _, unit in covered])
        label = units[0].info.volume_label
        if item_id is None:
            item_id = new_ulid()
            connection.execute(
                sa.insert(export.export_item).values(
                    id=item_id, edition_id=self._edition_id, kind="volume" if label else "bundle",
                    frozen_attrs={"volume_label": label} if label else {},
                    closed=payload.volume_closed,
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
                .values(closed=payload.volume_closed)
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
        if disposition_of(classify(error)) is Disposition.COUNT:
            self._update(request_id, state="abandoned")
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

    def retry_uncovered(self, request_id: str) -> str | None:
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
            return self._open(connection, units) if units else None

    def _update(self, request_id: str, **values: object) -> None:
        table = acquisition.download_request
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(table)
                .where(table.c.id == request_id)
                .values(updated_at=self._clock.now(), **values)
            )
