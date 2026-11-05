"""L18, EF-SRC-026 (c): per-series freshness, `series.stale` with k = 3 by default."""

import pytest

from tests.follow_env import Bench, follow_bench


def _seed(bench: Bench) -> None:
    """Three discoveries a week apart (cadence 7 days), the last one 0 days ago."""
    bench.run()
    bench.set_first_seen({"ch-001": 14, "ch-002": 7})
    bench.add_chapter(3)
    bench.run()


@pytest.mark.req("EF-SRC-026")
def test_series_stale_m1a_default_k3() -> None:
    with follow_bench() as bench:
        _seed(bench)
        bench.clock.advance(days=20)  # 20 < 3 x 7: still fresh
        assert bench.cycle.check_freshness() == []
        assert bench.events("series.stale") == []

        bench.clock.advance(days=2)  # 22 > 21: stale
        assert bench.cycle.check_freshness() == [bench.edition_id]
        assert [e["edition_id"] for e in bench.events("series.stale")] == [bench.edition_id]
        assert any(e.type == "series.stale" for e in bench.delivered)

        assert bench.cycle.check_freshness() == []  # announced once per episode
        assert len(bench.events("series.stale")) == 1

        bench.clock.advance(days=1)
        bench.add_chapter(4)  # a chapter arrives: the episode ends...
        bench.run()
        bench.clock.advance(days=22)
        assert bench.cycle.check_freshness() == [bench.edition_id]  # ...and may start again
        assert len(bench.events("series.stale")) == 2


@pytest.mark.req("EF-SRC-026")
def test_stale_threshold_has_a_14_day_floor_without_measurable_cadence() -> None:
    with follow_bench() as bench:
        bench.run()  # one discovery instant: no cadence
        bench.clock.advance(days=13)
        assert bench.cycle.check_freshness() == []
        bench.clock.advance(days=2)
        assert bench.cycle.check_freshness() == [bench.edition_id]


@pytest.mark.req("EF-SRC-026")
def test_finished_or_paused_series_are_never_stale() -> None:
    with follow_bench() as bench:
        bench.run()
        bench.clock.advance(days=40)
        with bench.lib.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE series_follow SET check_override = 'completed'")
        assert bench.cycle.check_freshness() == []
