"""Review of PR #51 (L19): confirmation bound to the estimate, ranges, extras, baseline."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.follow import _contested
from shelfhold.acquisition.application.planning import known_units, pending
from shelfhold.acquisition.public import (
    Backfill,
    BackfillConfirmationRequired,
    RequestBudget,
    suppress_follow,
)
from shelfhold.catalog.public import place_key
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import record_replica
from shelfhold.sdk.refs import UnitInfo
from tests.follow_env import START, follow_bench, new_chapter
from tests.integration.test_follow_policy import _serve, _ticketed, _watermark
from tests.integration.test_range_tickets import _bench
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.library_env import lib_env

MB = 10**6


def _fill_units(bench: object, count: int, *, suffix: str = "") -> None:
    with bench.lib.engine.begin() as connection:  # type: ignore[attr-defined]
        for n in range(1, count + 1):
            add_variant(
                connection,
                add_unit(connection, bench.edition_id, f"{n}{suffix}"),  # type: ignore[attr-defined]
                bench.binding_id,  # type: ignore[attr-defined]
                f"synthetic:{n}{suffix}",
            )


@pytest.mark.req("EF-EXP-053")
def test_the_confirmation_is_the_figures_the_user_saw() -> None:
    with follow_bench() as bench:
        _watermark(bench, "150")
        _fill_units(bench, 150)
        backfill = Backfill(bench.lib.engine, free_bytes=lambda: 10**12, unit_bytes=MB)
        for seen in ((None, None), (100, 150 * MB), (150, 100 * MB)):  # stale or partial
            with pytest.raises(BackfillConfirmationRequired):
                backfill.start(
                    bench.edition_id, "all", confirm_units=seen[0], confirm_bytes=seen[1]
                )
        assert (
            backfill.start(bench.edition_id, "all", confirm_units=150, confirm_bytes=150 * MB).units
            == 150
        )


@pytest.mark.req("EF-EXP-053")
def test_later_arrivals_beyond_the_confirmed_ceiling_stop_the_backfill() -> None:
    with follow_bench() as bench:
        _watermark(bench, "150")
        _fill_units(bench, 150)
        Backfill(bench.lib.engine, free_bytes=lambda: 10**12, unit_bytes=MB).start(
            bench.edition_id, "all", confirm_units=150, confirm_bytes=150 * MB
        )
        _fill_units(bench, 60, suffix=".5")  # sixty more holes appear below the watermark
        assert bench.run().requested == 0  # nothing leaves: a new confirmation is needed
        assert bench.rows("SELECT backfill_mode AS m FROM series_follow")[0]["m"] == "none"
        assert bench.rows("SELECT id FROM download_request") == []


@pytest.mark.req("EF-SRC-034")
def test_a_range_is_asked_whole_or_waits_whole_and_a_retry_counts_too() -> None:
    with lib_env() as lib:
        bench = _bench(lib, chapters=30)
        budget = RequestBudget(service_cap=50, key_cap=40)
        assert budget.take(bench.tickets._series.source_key, 30) == 30
        later = bench.tickets.request(bench.span(1, 15), budget)  # 10 left: 15 do not fit
        assert later.ticket_ids == [] and len(later.deferred) == 15
        assert bench.rows("SELECT id FROM download_request") == []

        done = bench.tickets.request(bench.span(1, 8), RequestBudget())
        real = bench.service.fetch

        async def partial(f: object, ticket: object) -> object:
            payload = await real(f, ticket)  # type: ignore[arg-type]
            return payload.model_copy(update={"covers": payload.covers[:5]})  # type: ignore[attr-defined]

        bench.service.fetch = partial  # type: ignore[method-assign]
        bench.advance()
        spent = RequestBudget(service_cap=50, key_cap=10)
        assert spent.take(bench.tickets._series.source_key, 8) == 8  # two left: 3 do not fit
        assert bench.tickets.retry_uncovered(done.ticket_ids[0], spent) is None  # 3 do not fit
        assert bench.tickets.retry_uncovered(done.ticket_ids[0], RequestBudget()) is not None


@pytest.mark.req("EF-BIB-044")
def test_plan_drops_asked_suppressed_and_held_units_and_never_doubles_an_item() -> None:
    with lib_env() as lib:
        bench = _bench(lib, chapters=10)
        everything = list(bench.units.values())
        with lib.engine.begin() as connection:
            suppress_follow(connection, bench.units[2].unit_id, actor_id=None, now=START)
            record_replica(
                connection, unit_id=bench.units[3].unit_id, location="library", content_hash=None,
                independent=True, physical_root_id=lib.library_root, rel_path="ch3.cbz",
            )  # fmt: skip
        first = bench.tickets.plan(everything)
        assert [[u.info.number for u in g] for g in first] == [
            ["1", "4", "5", "6", "7", "8"],
            ["9", "10"],
        ]
        bench.tickets.request(first[0], RequestBudget())
        assert [[u.info.number for u in g] for g in bench.tickets.plan(everything)] == [
            ["9", "10"]
        ]  # what is asked is not asked twice
        bench.advance()
        # a request that spans a closed volume and the next one: the closed item keeps its Units
        bench.tickets.request(bench.span(1, 10), RequestBudget())
        bench.advance()
        per_unit = bench.rows(
            "SELECT unit_id, count(*) AS n FROM unit_coverage GROUP BY unit_id HAVING n > 1"
        )
        assert per_unit == []


@pytest.mark.req("EF-BIB-028")
def test_contested_numbers_are_compared_normalized() -> None:
    assert place_key("10") == place_key("10.0") == place_key("Ch. 10")
    assert place_key("S2 10") != place_key("10")
    assert place_key(None, 3) != place_key(None, 4)
    infos = [
        UnitInfo(remote_unit_id="a", source_order=1, number="10"),
        UnitInfo(remote_unit_id="b", source_order=2, number="10.0"),
        UnitInfo(remote_unit_id="c", source_order=3, number="11"),
    ]
    assert _contested(infos) == {place_key("10")}


@pytest.mark.req("EF-EXP-053")
def test_an_extra_without_number_held_back_is_asked_again_but_not_the_first_look_ones() -> None:
    with follow_bench() as bench:
        assert bench.run().requested == 0  # first look: watermark 2, stamped at START
        with bench.lib.engine.begin() as connection:
            for order, label in ((90, "old"), (91, "new")):
                unit = add_unit(
                    connection, bench.edition_id, "", number_raw=label, track="unnumbered",
                    number_sort=None, source_order=order, kind="chapter",
                )  # fmt: skip
                variant = add_variant(connection, unit, bench.binding_id, f"synthetic:{label}")
                seen = START if label == "old" else START + timedelta(hours=1)
                connection.execute(
                    sa.text("UPDATE unit_variant SET first_seen_at = :s WHERE id = :v"),
                    {"s": seen, "v": variant},
                )
        with bench.lib.engine.connect() as connection:
            extras = [u.unit_id for u in pending(connection, bench.edition_id, "follow")]
        assert len(extras) == 1  # only the one that appeared after the first look


@pytest.mark.req("EF-EXP-053")
def test_a_first_look_with_only_extras_does_not_swallow_the_first_numbered_chapters() -> None:
    with follow_bench() as bench:
        extra = new_chapter(8001, 1, "/series-alpha/extra")
        extra["chapterNumber"] = None
        extra["name"] = "Prologue"
        _serve(bench, [extra])
        assert bench.run().requested == 0
        mark = bench.rows("SELECT follow_from_number AS n FROM series_follow")[0]["n"]
        assert Decimal(mark) < 0  # nothing numbered yet: any chapter will be beyond it
        _serve(bench, [extra, new_chapter(8002, 1)])
        assert bench.run().requested == 1
        assert [t.split(":")[0] for t in _ticketed(bench)] == ["1"]


@pytest.mark.req("EF-BIB-044")
def test_planning_reads_live_variants_and_tickets_of_its_own_edition_only() -> None:
    with follow_bench() as bench:
        statements: list[str] = []
        sa.event.listen(
            bench.lib.engine,
            "before_cursor_execute",
            lambda _c, _cur, statement, *_a: statements.append(statement),
        )
        with bench.lib.engine.connect() as connection:
            known_units(connection, bench.edition_id)
        scoped = [s for s in statements if "FROM unit_variant" in s or "download_request_unit" in s]
        assert scoped and all("edition_id" in s for s in scoped)
        assert export  # the export tables are read by the presence rule only
