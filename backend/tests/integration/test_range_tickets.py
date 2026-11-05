"""EF-SRC-032 / EF-SRC-034: one ticket and one export_item for a range of Units."""

import asyncio
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import RangeTickets, RangeUnit, RequestBudget
from shelfhold.library.public import Received
from shelfhold.sdk.refs import RangedPayload, SeriesRef, UnitInfo
from shelfhold.sdk.testing import FakeRangedConnectedService, fake_ranged_fetcher
from shelfhold.sdk.testing.fakes import FAKE_KEY
from tests.fakes_clock import ManualClock
from tests.follow_env import START
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.library_env import LibEnv, lib_env

SERIES = SeriesRef(source_key=FAKE_KEY, remote_id="series/fake-novel", title="Fake Novel")


class _Service(FakeRangedConnectedService):
    """The fake plugin with more chapters than its ten (volume 2 keeps growing)."""

    def __init__(self, chapters: int = 10) -> None:
        super().__init__()
        self.chapters = chapters

    async def list_units(self, f: Any, ref: SeriesRef) -> list[UnitInfo]:
        units = await super().list_units(f, ref)
        for n in range(len(units) + 1, self.chapters + 1):
            units.append(
                UnitInfo(
                    remote_unit_id=f"{FAKE_KEY}:ch-{n}", source_order=n, number=str(n),
                    number_sort=Decimal(n), volume_label="Vol.2", volume_closed=False,
                )
            )  # fmt: skip
        return units


@dataclass
class Bench:
    lib: LibEnv
    service: _Service
    tickets: RangeTickets
    units: dict[int, RangeUnit]
    kept: list[tuple[str, int]] = field(default_factory=list)

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.lib.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def advance(self, times: int = 3) -> None:
        for _ in range(times):  # queued, downloading, ready
            asyncio.run(self.tickets.advance())

    def span(self, first: int, last: int) -> list[RangeUnit]:
        return [self.units[n] for n in range(first, last + 1)]


def _bench(lib: LibEnv, chapters: int = 10) -> Bench:
    service = _Service(chapters)
    fetcher = fake_ranged_fetcher()
    infos = asyncio.run(service.list_units(fetcher, SERIES))
    units: dict[int, RangeUnit] = {}
    with lib.engine.begin() as connection:
        for info in infos:
            number = int(info.number or 0)
            unit_id = add_unit(connection, lib.chain["edition"], str(number + 1))  # 1 is taken
            add_variant(connection, unit_id, lib.chain["binding"], info.remote_unit_id)
            units[number] = RangeUnit(unit_id, info)
    kept: list[tuple[str, int]] = []

    @asynccontextmanager
    async def receive(asset: Any) -> Any:
        yield Received(
            path=Path("/nonexistent") / asset.filename, size=7, sha256="sha256:" + "0" * 64
        )

    def keep(received: Received, payload: RangedPayload, item_id: str) -> None:
        kept.append((item_id, len(payload.covers)))

    tickets = RangeTickets(
        lib.engine, ManualClock(START), service, fetcher, receive, keep,
        edition_id=lib.chain["edition"], binding_id=lib.chain["binding"], series=SERIES,
    )  # fmt: skip
    return Bench(lib, service, tickets, units, kept)


@pytest.fixture
def bench() -> Iterator[Bench]:
    with lib_env() as lib:
        yield _bench(lib)


@pytest.mark.req("EF-SRC-034")
def test_range_request_one_ticket_one_export_item(bench: Bench) -> None:
    result = bench.tickets.request(bench.span(1, 8), RequestBudget())
    assert len(result.ticket_ids) == 1 and result.deferred == []
    assert len(bench.rows("SELECT id FROM download_request")) == 1  # one ticket...
    request = bench.rows("SELECT target_kind, variant_id, range_ref FROM download_request")[0]
    assert request["target_kind"] == "range" and request["variant_id"] is None
    assert len(bench.rows("SELECT unit_id FROM download_request_unit")) == 8  # ...eight Units
    bench.advance()
    items = bench.rows("SELECT id, kind, closed FROM export_item")
    assert len(items) == 1 and items[0]["closed"] == 1  # the source announced the volume closed
    coverage = bench.rows(
        "SELECT unit_id, origin, start_locator FROM unit_coverage WHERE carrier_id = :i "
        "ORDER BY position",
        i=items[0]["id"],
    )
    assert [c["unit_id"] for c in coverage] == [bench.units[n].unit_id for n in range(1, 9)]
    assert {c["origin"] for c in coverage} == {"provider"} and coverage[0]["start_locator"]
    assert {r["state"] for r in bench.rows("SELECT state FROM download_request_unit")} == {
        "covered"
    }
    assert bench.rows("SELECT state FROM download_request")[0]["state"] == "fetched"
    assert bench.kept == [(items[0]["id"], 8)]


@pytest.mark.req("EF-SRC-034")
def test_range_request_cap_counted_in_units() -> None:
    with lib_env() as lib:
        bench = _bench(lib, chapters=60)
        budget = RequestBudget(service_cap=50, key_cap=50)
        first = bench.tickets.request(bench.span(1, 60), budget)
        assert len(first.ticket_ids) == 1
        assert len(bench.rows("SELECT unit_id FROM download_request_unit")) == 50
        assert [u.unit_id for u in first.deferred] == [
            bench.units[n].unit_id for n in range(51, 61)
        ]
        second = bench.tickets.request(first.deferred, RequestBudget(service_cap=50, key_cap=50))
        assert len(second.ticket_ids) == 1 and second.deferred == []
        assert len(bench.rows("SELECT id FROM download_request")) == 2
        assert len(bench.rows("SELECT unit_id FROM download_request_unit")) == 60


@pytest.mark.req("EF-SRC-034")
def test_retry_only_uncovered_units_of_ticket(bench: Bench) -> None:
    real = bench.service.fetch

    async def partial(f: Any, ticket: Any) -> Any:
        payload = await real(f, ticket)
        return payload.model_copy(update={"covers": payload.covers[:5]})  # 1..5 only

    bench.service.fetch = partial  # type: ignore[method-assign]
    result = bench.tickets.request(bench.span(1, 8), RequestBudget())
    bench.advance()
    states = {
        r["unit_id"]: r["state"]
        for r in bench.rows("SELECT unit_id, state FROM download_request_unit")
    }
    assert [states[bench.units[n].unit_id] for n in range(1, 9)] == ["covered"] * 5 + [
        "missing"
    ] * 3

    retry = bench.tickets.retry_uncovered(result.ticket_ids[0], RequestBudget())
    assert retry is not None and retry != result.ticket_ids[0]
    asked = bench.rows(
        "SELECT unit_id FROM download_request_unit WHERE download_request_id = :r", r=retry
    )
    assert {a["unit_id"] for a in asked} == {bench.units[n].unit_id for n in (6, 7, 8)}
    assert (
        bench.tickets.retry_uncovered(result.ticket_ids[0], RequestBudget()) is None
    )  # already taken over


@pytest.mark.req("EF-SRC-032")
def test_n_chapters_one_volume_only_rolling_volume_changes() -> None:
    with lib_env() as lib:
        bench = _bench(lib, chapters=10)
        groups = bench.tickets.plan(list(bench.units.values()))
        assert [(g[0].info.volume_label, len(g)) for g in groups] == [("Vol.1", 8), ("Vol.2", 2)]
        for group in groups:
            bench.tickets.request(group, RequestBudget())
        bench.advance()
        before = {r["id"]: r for r in bench.rows("SELECT id, closed FROM export_item")}
        assert len(before) == 2 and sorted(r["closed"] for r in before.values()) == [0, 1]
        closed_id = next(i for i, r in before.items() if r["closed"])
        closed_coverage = bench.rows(
            "SELECT unit_id FROM unit_coverage WHERE carrier_id = :i ORDER BY position", i=closed_id
        )

        # chapter 11 arrives in the rolling volume
        bench.service.chapters = 11
        with lib.engine.begin() as connection:
            unit_id = add_unit(connection, lib.chain["edition"], "12")
            add_variant(connection, unit_id, lib.chain["binding"], f"{FAKE_KEY}:ch-11")
        infos = asyncio.run(bench.service.list_units(fake_ranged_fetcher(), SERIES))
        everything = [
            *bench.units.values(),
            RangeUnit(unit_id, infos[-1]),
        ]
        again = bench.tickets.plan(everything)
        assert [(g[0].info.volume_label, len(g)) for g in again] == [("Vol.2", 3)]  # not Vol.1
        bench.tickets.request(again[0], RequestBudget())
        bench.advance()

        after = {r["id"]: r for r in bench.rows("SELECT id, closed FROM export_item")}
        assert set(after) == set(before)  # no new item: the rolling one was updated in place
        assert (
            bench.rows(
                "SELECT unit_id FROM unit_coverage WHERE carrier_id = :i ORDER BY position",
                i=closed_id,
            )
            == closed_coverage
        )  # the closed volume is untouched
        rolling = next(i for i, r in after.items() if not r["closed"])
        assert (
            len(bench.rows("SELECT unit_id FROM unit_coverage WHERE carrier_id = :i", i=rolling))
            == 3
        )
