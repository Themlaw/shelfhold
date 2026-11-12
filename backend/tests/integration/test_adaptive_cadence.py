"""EF-SRC-038 (M2 L29): the adaptive check cadence in the piloted follow cycle, "check now", and
the always-on mode where the cadence only feeds `series.stale`."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.cadence import (
    CadencedFollowCycle,
    SeriesPausedError,
    UnknownSeriesError,
    check_now,
    make_all_due,
)
from shelfhold.acquisition.application.follow import FOLLOW_JOB_KIND, REFRESH_SPACING
from shelfhold.jobs.huey_app import JobPools
from tests.follow_env import Bench, follow_bench

DAY = timedelta(days=1)


def _weekly(bench: Bench) -> None:
    """Three discoveries a week apart, the last one now; one look done, so the cadence is known."""
    bench.run()
    bench.set_first_seen({"ch-001": 14, "ch-002": 7})
    bench.add_chapter(3)
    bench.run()


def _expected(bench: Bench, binding_id: str | None = None) -> dict[str, str]:
    return bench.rows(
        "SELECT next_expected, check_cron FROM source_binding WHERE id = :b",
        b=binding_id or bench.binding_id,
    )[0]


@pytest.mark.req("EF-SRC-038")
def test_adaptive_check_interval_from_observed_cadence() -> None:
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        _weekly(bench)
        row = _expected(bench)
        assert row["check_cron"]
        wait = _expected_at(row) - bench.clock.now()
        assert timedelta(hours=1) <= wait <= 7 * DAY
        assert wait >= 6 * DAY  # a weekly series: around the next week, not tomorrow


def _expected_at(row: dict[str, str]) -> datetime:
    return datetime.fromisoformat(row["next_expected"]).replace(tzinfo=UTC)


@pytest.mark.req("EF-SRC-038")
def test_series_is_not_refreshed_before_its_expected_release() -> None:
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        _weekly(bench)
        asked = len(bench.server.refreshed)
        bench.clock.advance(days=2)
        bench.run()
        assert len(bench.server.refreshed) == asked  # Suwayomi is not asked: nothing is due
        bench.clock.advance(days=6)
        bench.run()
        assert len(bench.server.refreshed) == asked + 1  # the week has passed: asked once


@pytest.mark.req("EF-SRC-038")
def test_finished_series_checked_rarely() -> None:
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        bench.rows("SELECT 1")
        with bench.lib.engine.begin() as c:
            c.execute(sa.text("UPDATE edition SET upstream_status = 'finished'"))
        bench.run()
        first = len(bench.server.refreshed)
        assert first == 1
        for _ in range(5):  # attack: a cycle every few days for 25 days
            bench.clock.advance(days=5)
            bench.run()
        assert len(bench.server.refreshed) == first  # never inside the 30 days
        bench.clock.advance(days=6)
        bench.run()
        assert len(bench.server.refreshed) == first + 1


@pytest.mark.req("EF-SRC-038")
def test_series_marked_completed_by_hand_is_rarely_checked_too() -> None:
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        with bench.lib.engine.begin() as c:
            c.execute(sa.text("UPDATE series_follow SET check_override = 'completed'"))
        bench.run()
        bench.clock.advance(days=10)
        bench.run()
        assert len(bench.server.refreshed) == 1


@pytest.mark.req("EF-SRC-038")
def test_paused_series_is_never_checked_not_even_now() -> None:
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        with bench.lib.engine.begin() as c:
            c.execute(sa.text("UPDATE series_follow SET check_override = 'paused'"))
        bench.run()
        assert bench.server.refreshed == []
        with bench.lib.engine.begin() as c, pytest.raises(SeriesPausedError):
            check_now(c, bench.edition_id, bench.clock.now())
        bench.run()
        assert bench.server.refreshed == []


@pytest.mark.req("EF-SRC-038", "EF-SRC-009")
def test_check_now_respects_limit_key() -> None:
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        twin = {**bench.alpha(), "id": 190, "url": "/series-delta", "title": "Series Delta"}
        twin["chapters"] = []
        bench.server.state["mangas"].append(twin)  # a second series of the same upstream source
        bench.add_binding("suwayomi:7001", "/series-delta")
        bench.add_binding("suwayomi:7002", "/series-beta")  # another source
        bench.run()
        bench.sleeps.clear()
        asked = len(bench.server.refreshed)
        bench.clock.advance(hours=2)
        bench.run()
        assert len(bench.server.refreshed) == asked  # nothing is due yet
        editions = [
            r["edition_id"]
            for r in bench.rows(
                "SELECT b.edition_id FROM source_binding b "
                "JOIN series_follow f ON f.edition_id = b.edition_id"
            )
        ]
        with bench.lib.engine.begin() as c:
            for edition in editions:
                check_now(c, edition, bench.clock.now())
        bench.run()
        assert len(bench.server.refreshed) == asked + 3  # all three were asked
        spaced = [s for s in bench.sleeps if s >= REFRESH_SPACING]
        assert len(spaced) == 1  # the second series of the same source waited its spacing


@pytest.mark.req("EF-SRC-038")
def test_check_now_asks_only_the_requested_series() -> None:
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        bench.add_binding("suwayomi:7002", "/series-beta")
        bench.run()
        asked = len(bench.server.refreshed)
        bench.clock.advance(hours=2)  # the source was not asked a moment ago
        with bench.lib.engine.begin() as c:
            check_now(c, bench.edition_id, bench.clock.now())
        bench.run()
        assert len(bench.server.refreshed) == asked + 1


@pytest.mark.req("EF-SRC-038")
def test_check_now_on_a_finished_series_checks_once_and_returns_to_the_rare_pace() -> None:
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        with bench.lib.engine.begin() as c:
            c.execute(sa.text("UPDATE edition SET upstream_status = 'finished'"))
        bench.run()
        asked = len(bench.server.refreshed)
        bench.clock.advance(hours=2)  # the source was not asked a moment ago
        with bench.lib.engine.begin() as c:
            check_now(c, bench.edition_id, bench.clock.now())
        bench.run()
        assert len(bench.server.refreshed) == asked + 1
        bench.run()
        assert len(bench.server.refreshed) == asked + 1  # one look per request, not a habit


@pytest.mark.req("EF-SRC-038")
def test_check_now_of_an_unknown_series_is_refused() -> None:
    with (
        follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench,
        bench.lib.engine.begin() as c,
        pytest.raises(UnknownSeriesError),
    ):
        check_now(c, "01NOSUCHEDITION00000000000", bench.clock.now())


@pytest.mark.req("EF-SRC-038")
def test_always_on_mode_cadence_only_feeds_series_stale() -> None:
    with follow_bench(check_mode="always_on", cycle_class=CadencedFollowCycle) as bench:
        _weekly(bench)
        with bench.lib.engine.begin() as c:
            check_now(
                c, bench.edition_id, bench.clock.now()
            )  # even asked, Shelfhold refreshes nothing
        bench.clock.advance(days=8)
        bench.run()
        assert bench.server.refreshed == []
        assert not {"fetchChapters", "updateLibrary"} & {
            f for r in bench.server.requests for f in r.fields
        }
        bench.clock.advance(days=22)
        assert bench.cycle.check_freshness() == [bench.edition_id]  # the cadence still feeds it
        assert [e["count"] for e in bench.events("series.stale")] == [1]


@pytest.mark.req("EF-SRC-038", "EF-INT-017")
def test_check_now_job_is_of_the_news_class(tmp_path: Path) -> None:
    from shelfhold.acquisition.application.cadence import request_check_job

    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        (tmp_path / "data").mkdir(exist_ok=True)
        pools = JobPools(tmp_path / "data", engine=bench.lib.engine, clock=bench.clock, seed="c")
        pools.register(FOLLOW_JOB_KIND, pool="io", handler=lambda _context: None)
        request_check_job(bench.lib.engine, bench.clock, pools, bench.edition_id)
        jobs = bench.rows("SELECT kind, priority_class FROM job_view")
        assert jobs == [{"kind": FOLLOW_JOB_KIND, "priority_class": "new"}]
        request_check_job(bench.lib.engine, bench.clock, pools, bench.edition_id)  # a double click
        assert len(bench.rows("SELECT id FROM job_view")) == 1


@pytest.mark.req("EF-SRC-038", "EF-SRC-009")
def test_check_now_clicked_ten_times_is_one_look_of_the_source() -> None:
    """Attack: a source asked less than `check_min` ago is not asked again on request."""
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        bench.run()
        asked = len(bench.server.refreshed)
        before = _expected(bench)
        for _ in range(10):
            with bench.lib.engine.begin() as c:
                assert check_now(c, bench.edition_id, bench.clock.now()) == []
            bench.run()
        assert len(bench.server.refreshed) == asked
        assert _expected(bench) == before  # not even made due
        bench.clock.advance(hours=2)
        with bench.lib.engine.begin() as c:
            assert check_now(c, bench.edition_id, bench.clock.now()) == ["suwayomi"]
        bench.run()
        assert len(bench.server.refreshed) == asked + 1


@pytest.mark.req("EF-SRC-038")
def test_series_scheduled_absurdly_far_away_is_due_again() -> None:
    """Attack: a clock set back, or `check_max` lowered, cannot starve a series."""
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        bench.run()
        asked = len(bench.server.refreshed)
        far = bench.clock.now() + 400 * DAY
        with bench.lib.engine.begin() as c:
            c.execute(sa.text("UPDATE source_binding SET next_expected = :f"), {"f": far})
        bench.run()
        assert len(bench.server.refreshed) == asked + 1


@pytest.mark.req("EF-SRC-038")
def test_manual_follow_run_leaves_finished_and_paused_series_alone() -> None:
    with follow_bench(check_mode="piloted", cycle_class=CadencedFollowCycle) as bench:
        bench.add_binding("suwayomi:7002", "/series-beta")
        bench.run()
        far = bench.clock.now() + 20 * DAY
        with bench.lib.engine.begin() as c:
            c.execute(sa.text("UPDATE source_binding SET next_expected = :f"), {"f": far})
            c.execute(
                sa.text("UPDATE edition SET upstream_status = 'finished' WHERE id = :e"),
                {"e": bench.edition_id},
            )
            make_all_due(c, bench.clock.now())
        rows = {
            r["edition_id"]: r["next_expected"]
            for r in bench.rows("SELECT edition_id, next_expected FROM source_binding")
        }
        assert str(far.replace(tzinfo=None))[:10] in rows[bench.edition_id]  # finished: untouched
        others = [v for k, v in rows.items() if k != bench.edition_id]
        assert others and all(str(far.replace(tzinfo=None))[:10] not in v for v in others)
        with bench.lib.engine.begin() as c:
            c.execute(sa.text("UPDATE series_follow SET check_override = 'paused'"))
            c.execute(sa.text("UPDATE source_binding SET next_expected = :f"), {"f": far})
            make_all_due(c, bench.clock.now())
        assert all(
            str(far.replace(tzinfo=None))[:10] in r["next_expected"]
            for r in bench.rows(
                "SELECT b.next_expected FROM source_binding b "
                "JOIN series_follow f ON f.edition_id = b.edition_id"
            )
        )
