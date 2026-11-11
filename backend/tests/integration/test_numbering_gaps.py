"""EF-BIB-056 (and the `series.gap` event of EF-INT-006): holes beyond the watermark, looked for
on the other bindings, told once, never blocking, never a loop of requests."""

import pytest
import sqlalchemy as sa

from tests.filters_env import asked, filter_bench, gaps, listing
from tests.follow_env import Bench
from tests.multi_source_env import ALPHA, BETA, requests, serve, settle

pytestmark = pytest.mark.req("EF-BIB-056")


def _told(bench: Bench) -> list[dict[str, object]]:
    return bench.events("series.gap")


def test_gap_searched_on_other_bindings() -> None:
    """10 and 12 at rank 1, 11 at rank 2 only: 11 is asked of rank 2 and is no gap."""
    with filter_bench() as bench:
        serve(bench, ALPHA, listing(ALPHA, [10, 12]))
        serve(bench, BETA, listing(BETA, [10, 11, 12]))
        bench.run()
        assert [(n, s) for n, s, _ in requests(bench)] == [
            ("10", "alpha"), ("11", "beta"), ("12", "alpha"),
        ]  # fmt: skip
        settle(bench)
        assert gaps(bench) == [] and _told(bench) == []


def test_gap_reported_once_not_blocking() -> None:
    with filter_bench() as bench:
        serve(bench, ALPHA, listing(ALPHA, [10, 12]))
        serve(bench, BETA, listing(BETA, [10, 12]))
        first = bench.run()
        assert first.gaps == 1
        assert gaps(bench) == [("main", "", "11")]
        assert len(_told(bench)) == 1
        assert [n for n, _, _ in requests(bench)] == ["10", "12"]  # not blocking: both asked
        for _ in range(4):  # and not told again, cycle after cycle
            assert bench.run().gaps == 0
        assert len(_told(bench)) == 1
        serve(bench, ALPHA, listing(ALPHA, [10, 12, 13]))  # the series goes on
        bench.run()
        assert "13" in [n for n, _, _ in requests(bench)]
        assert len(_told(bench)) == 1


def test_gap_costs_no_request_in_any_cycle() -> None:
    """A place that exists nowhere is never asked for, however many cycles go by."""
    with filter_bench() as bench:
        serve(bench, ALPHA, listing(ALPHA, [10, 14]))
        serve(bench, BETA, listing(BETA, [10, 14]))
        bench.run()
        total = len(asked(bench))
        assert gaps(bench) == [("main", "", "11"), ("main", "", "12"), ("main", "", "13")]
        for _ in range(5):
            bench.run()
        assert len(asked(bench)) == total
        assert len(_told(bench)) == 1  # one event for the series, naming how many
        assert _told(bench)[0]["count"] == 3


def test_decimal_numbers_never_create_gap() -> None:
    with filter_bench(two_bindings=False) as bench:
        serve(bench, ALPHA, listing(ALPHA, [10, 10.5, 11]))
        bench.run()
        settle(bench)
        assert gaps(bench) == [] and _told(bench) == []


def test_decimal_does_not_fill_the_next_whole_place() -> None:
    """10.5 and 12 leave 11 missing: a decimal is not a whole place."""
    with filter_bench(two_bindings=False) as bench:
        serve(bench, ALPHA, listing(ALPHA, [10.5, 12]))
        bench.run()
        assert gaps(bench) == [("main", "", "11")]


def test_duplicate_numbers_never_create_gap() -> None:
    """The same chapter offered twice (two teams, two bindings) is one place."""
    with filter_bench() as bench:
        serve(bench, ALPHA, listing(ALPHA, [10, 11], team="A") + listing(ALPHA, [11, 12], team="B"))
        serve(bench, BETA, listing(BETA, [10, 11, 12]))
        bench.run()
        settle(bench)
        assert gaps(bench) == [] and _told(bench) == []


def test_places_below_the_watermark_are_not_gaps() -> None:
    """Below the watermark nothing changes: `backfill --gaps` is the explicit action."""
    with filter_bench(two_bindings=False) as bench:
        with bench.lib.engine.begin() as connection:
            connection.execute(sa.text("UPDATE series_follow SET follow_from_number = '20'"))
        serve(bench, ALPHA, listing(ALPHA, [3, 9, 15]))
        bench.run()
        assert gaps(bench) == [] and _told(bench) == []


def test_part_of_a_chapter_never_creates_gap() -> None:
    with filter_bench(two_bindings=False) as bench:
        listed = listing(ALPHA, [10, 11, 12])
        listed[1]["name"] = "Chapter 11 part 1"
        listed.append({**listed[1], "id": 6999, "url": "/series-alpha/ch-11-p2",
                       "name": "Chapter 11 part 2", "sourceOrder": 9})  # fmt: skip
        serve(bench, ALPHA, listed)
        bench.run()
        assert gaps(bench) == []


def test_tracks_and_segments_are_not_mixed() -> None:
    """Extra 1 and Extra 3 of one track do not make the main track's places a gap."""
    edits = {"/series-alpha": lambda u: u.model_copy(update={"number": f"{u.number} extra"})
             if u.number in ("1", "3") else u}  # fmt: skip
    with filter_bench(edits, two_bindings=False) as bench:
        serve(bench, ALPHA, listing(ALPHA, [1, 3, 10, 11]))
        bench.run()
        assert gaps(bench) == [("extra", "", "2")]  # per track: main 10 and 11 hold no hole


def test_gap_ack_silences_gap() -> None:
    with filter_bench(two_bindings=False) as bench:
        serve(bench, ALPHA, listing(ALPHA, [10, 12]))
        bench.run()
        assert gaps(bench) == [("main", "", "11")]
        with bench.lib.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO gap_ack (edition_id, track, number_segment, number_norm, acked_at)"
                    " VALUES (:e, 'main', '', '11', :t)"
                ),
                {"e": bench.edition_id, "t": bench.clock.now()},
            )
        bench.run()
        assert gaps(bench) == []  # gone from the series' page
        for _ in range(3):
            bench.run()
        assert len(_told(bench)) == 1  # and never told again


def test_gap_filled_later_leaves_the_list() -> None:
    with filter_bench() as bench:
        serve(bench, ALPHA, listing(ALPHA, [10, 12]))
        serve(bench, BETA, listing(BETA, [10, 12]))
        bench.run()
        assert gaps(bench) == [("main", "", "11")]
        serve(bench, BETA, listing(BETA, [10, 11, 12]))  # the second source gets it later
        bench.run()
        settle(bench)
        assert gaps(bench) == []
        assert ("11", "beta") in [(n, s) for n, s, _ in requests(bench)]


def test_gap_is_not_told_while_a_binding_was_not_read() -> None:
    """A binding that failed to answer may list the place: the gap waits for a full reading."""
    with filter_bench() as bench:
        serve(bench, ALPHA, listing(ALPHA, [10, 12]))
        serve(bench, BETA, listing(BETA, [10, 11, 12]))
        bench.failing.add("/series-beta")  # type: ignore[attr-defined]  # not read this cycle
        bench.run()
        assert _told(bench) == [] and gaps(bench) == []
        bench.failing.clear()  # type: ignore[attr-defined]
        bench.run()  # read in full now: beta has 11, nothing is missing
        settle(bench)
        assert _told(bench) == [] and gaps(bench) == []


def test_upstream_renumbering_neither_loops_nor_hides() -> None:
    """The source renumbers (11 becomes 12): the old place is not asked again and the cycle
    settles."""
    with filter_bench(two_bindings=False) as bench:
        serve(bench, ALPHA, listing(ALPHA, [10, 11, 12]))
        bench.run()
        settle(bench)
        before = len(asked(bench))
        serve(bench, ALPHA, listing(ALPHA, [10, 12, 13]))
        for _ in range(5):
            bench.run()
        assert len(asked(bench)) <= before + 1  # at most the new 13
        assert len(_told(bench)) <= 1


def test_gap_scan_is_bounded_on_absurd_numbers() -> None:
    with filter_bench(two_bindings=False) as bench:
        serve(bench, ALPHA, listing(ALPHA, [10, 999999]))
        bench.run()
        assert 0 < len(gaps(bench)) <= 200
