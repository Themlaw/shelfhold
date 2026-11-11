"""EF-SRC-039: the download filters decide before the request, never after the download."""

from typing import Any

import pytest
import sqlalchemy as sa

from tests.filters_env import (
    asked,
    extra_when,
    filter_bench,
    lang_of,
    listing,
    reasons,
    set_filters,
)
from tests.follow_env import Bench
from tests.multi_source_env import ALPHA, BETA, requests, serve, settle

pytestmark = pytest.mark.req("EF-SRC-039")


def _tickets(bench: Bench) -> int:
    return len(bench.rows("SELECT id FROM download_request"))


def test_language_filter_applied_before_request() -> None:
    """Chapter 3 is published in en (rank 1) and fr (rank 2): only the fr one is ever asked."""
    with filter_bench({"/series-alpha": lang_of("en"), "/series-beta": lang_of("fr")}) as bench:
        set_filters(bench, {"languages": ["fr"]})
        serve(bench, ALPHA, listing(ALPHA, [3]))
        serve(bench, BETA, listing(BETA, [3]))
        report = bench.run()
        assert report.requested == 1
        assert [(n, s) for n, s, _ in requests(bench)] == [("3", "beta")]
        assert asked(bench) == [7000 + 30 * 10 + 1]  # the simulated server's own counter
        settle(bench)
        assert len(asked(bench)) == 1  # and never the en one, in the cycles that follow
        assert reasons(bench, "3") == {"3": None}


def test_filtered_unit_costs_no_request_in_any_cycle() -> None:
    """The counter of the simulated server proves it: nothing is asked for a filtered Unit, the
    ticket is never opened, and the Unit stays visible with its reason."""
    with filter_bench({"/series-alpha": lang_of("en"), "/series-beta": lang_of("en")}) as bench:
        set_filters(bench, {"languages": ["fr"]})
        serve(bench, ALPHA, listing(ALPHA, [1, 2]))
        serve(bench, BETA, listing(BETA, [1, 2]))
        for _ in range(4):
            assert bench.run().requested == 0
        assert asked(bench) == []
        assert _tickets(bench) == 0
        assert reasons(bench, "1", "2") == {"1": "language", "2": "language"}


def test_language_filter_accepts_sub_tags_and_refuses_lookalikes() -> None:
    with filter_bench({"/series-alpha": lang_of("fr-CA"), "/series-beta": lang_of("frr")}) as bench:
        set_filters(bench, {"languages": ["fr"]})
        serve(bench, ALPHA, listing(ALPHA, [1]))
        serve(bench, BETA, listing(BETA, [2]))  # `frr` (North Frisian) is not `fr`
        bench.run()
        assert [(n, s) for n, s, _ in requests(bench)] == [("1", "alpha")]
        assert reasons(bench, "1", "2") == {"1": None, "2": "language"}


def test_extra_chapter_filtered_with_reason() -> None:
    with filter_bench({"/series-alpha": extra_when("12.5")}, two_bindings=False) as bench:
        set_filters(bench, {"exclude_extras": True})
        serve(bench, ALPHA, listing(ALPHA, [12, 12.5, 13]))
        bench.run()
        settle(bench)
        assert [n for n, _, _ in requests(bench)] == ["12", "13"]
        assert reasons(bench, "12", "12.5", "13") == {"12": None, "12.5": "extra", "13": None}
        assert len(asked(bench)) == 2


def test_extras_are_asked_when_the_filter_is_off() -> None:
    with filter_bench({"/series-alpha": extra_when("12.5")}, two_bindings=False) as bench:
        serve(bench, ALPHA, listing(ALPHA, [12, 12.5]))
        bench.run()
        assert [n for n, _, _ in requests(bench)] == ["12", "12.5"]
        assert reasons(bench, "12", "12.5") == {"12": None, "12.5": None}


def test_team_filters_refuse_before_the_team_election() -> None:
    """A refused team takes no part in the election: the accepted one is asked even when the
    refused one holds the better rank."""
    with filter_bench() as bench:
        set_filters(bench, {"teams_refuse": ["Team A"]})
        serve(bench, ALPHA, listing(ALPHA, [1], team="A"))
        serve(bench, BETA, listing(BETA, [1], team="B"))
        bench.run()
        assert [(n, s) for n, s, _ in requests(bench)] == [("1", "beta")]
        assert len(asked(bench)) == 1


def test_team_accept_list_refuses_every_other_team() -> None:
    with filter_bench(two_bindings=False) as bench:
        set_filters(bench, {"teams_accept": ["team b"]})
        serve(bench, ALPHA, listing(ALPHA, [1], team="A"))
        bench.run()
        settle(bench)
        assert asked(bench) == [] and _tickets(bench) == 0
        assert reasons(bench, "1") == {"1": "team"}


def test_per_cycle_cap_spills_to_backfill_class() -> None:
    """120 news and a cap of 50: 50 go out as news, the other 70 as catch-up, once every
    series' news is out, within the shared caps."""
    with filter_bench(
        two_bindings=False,
    ) as bench:
        set_filters(bench, None, cap=50)
        bench.cycle = bench.new_cycle(service_cap=500, key_cap=500)
        serve(bench, ALPHA, listing(ALPHA, list(range(1, 121))))
        report = bench.run()
        assert report.requested == 120 and report.spilled == 70
        assert _tickets(bench) == 120


def test_per_cycle_cap_defaults_to_fifty() -> None:
    with filter_bench(two_bindings=False) as bench:
        bench.cycle = bench.new_cycle(service_cap=500, key_cap=500)
        serve(bench, ALPHA, listing(ALPHA, list(range(1, 81))))
        report = bench.run()
        assert report.requested == 80 and report.spilled == 30


def test_spill_never_starves_the_news_of_another_series() -> None:
    """The surplus of a series is asked after the news of the others, not before."""
    with filter_bench(two_bindings=False) as bench:
        other = bench.add_binding("suwayomi:7002", "/series-beta")
        assert other
        set_filters(bench, None, cap=2)
        with bench.lib.engine.begin() as connection:
            connection.execute(sa.text("UPDATE series_follow SET follow_from_number = '0'"))
        serve(bench, ALPHA, listing(ALPHA, [1, 2, 3, 4, 5]))
        serve(bench, BETA, listing(BETA, [1, 2]))
        bench.cycle = bench.new_cycle(service_cap=6, key_cap=6)
        bench.run()
        order = [r["remote"] for r in bench.rows(
            "SELECT si.remote_id AS remote FROM download_request r "
            "JOIN source_binding b ON b.id = r.source_binding_id "
            "JOIN source_item si ON si.id = b.source_item_id ORDER BY r.rowid"
        )]  # fmt: skip
        assert order.count("/series-beta") == 2  # the other series' news all went out
        assert order.count("/series-alpha") == 4  # 2 news + 2 catch-up within the shared cap of 6


@pytest.mark.parametrize(
    "bad",
    [
        {"languages": "fr"},
        {"languages": ["not a tag!"]},
        {"teams_refuse": [3]},
        {"exclude_extras": "yes"},
        {"colour": "red"},  # a typo'd key is not a filter that does nothing
        {"cap": 0},
    ],
)
def test_unreadable_filters_refuse_everything_and_ask_nothing(bad: dict[str, Any]) -> None:
    with filter_bench(two_bindings=False) as bench:
        set_filters(bench, bad)
        serve(bench, ALPHA, listing(ALPHA, [1]))
        bench.run()
        bench.run()
        assert asked(bench) == [] and _tickets(bench) == 0
        assert reasons(bench, "1") == {"1": "invalid"}


def test_changing_the_filters_releases_what_they_held() -> None:
    with filter_bench({"/series-alpha": lang_of("en")}, two_bindings=False) as bench:
        set_filters(bench, {"languages": ["fr"]})
        serve(bench, ALPHA, listing(ALPHA, [1]))
        bench.run()
        assert asked(bench) == [] and reasons(bench, "1") == {"1": "language"}
        set_filters(bench, {"languages": ["en"]})
        bench.run()
        assert len(asked(bench)) == 1 and reasons(bench, "1") == {"1": None}


def test_filters_follow_the_series_not_the_service() -> None:
    """A series without filters of its own asks for everything: another one's do not leak."""
    with filter_bench({"/series-alpha": lang_of("en")}, two_bindings=False) as bench:
        other = bench.add_binding("suwayomi:7002", "/series-beta")
        assert other
        set_filters(bench, {"languages": ["fr"]})  # every followed series, then:
        with bench.lib.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE series_follow SET filters = NULL WHERE edition_id <> :e"),
                {"e": bench.edition_id},
            )
        serve(bench, ALPHA, listing(ALPHA, [1]))
        serve(bench, BETA, listing(BETA, [1]))
        with bench.lib.engine.begin() as connection:
            connection.execute(sa.text("UPDATE series_follow SET follow_from_number = '0'"))
        bench.run()
        assert len(asked(bench)) == 1  # the unfiltered series' chapter only


def test_fallback_never_asks_what_the_filters_refuse() -> None:
    """The rank-1 binding dies; the next rank takes over but its team is refused: nothing goes."""
    with filter_bench() as bench:
        set_filters(bench, {"teams_refuse": ["Team B"]})
        serve(bench, ALPHA, listing(ALPHA, [1, 2]))
        serve(bench, BETA, listing(BETA, [1, 2], team="B"))
        settle(bench)
        before = list(asked(bench))
        bench.alpha()["inLibrary"] = False
        bench.run()
        bench.run()  # removed upstream: the binding falls back to the next rank
        assert bench.binding()["disabled_reason"] == "removed_upstream"
        serve(bench, BETA, listing(BETA, [1, 2, 3], team="B"))
        bench.run()
        settle(bench)
        assert asked(bench) == before  # chapter 3 exists at the fallback, filtered
        assert reasons(bench, "3")["3"] == "team"


def test_fallback_asks_the_open_request_again_when_the_filters_allow_it() -> None:
    with filter_bench() as bench:
        serve(bench, ALPHA, listing(ALPHA, [1]))
        serve(bench, BETA, listing(BETA, [1, 2], team="B"))
        settle(bench)
        bench.alpha()["inLibrary"] = False
        bench.run()
        bench.run()
        settle(bench)
        assert [(n, s) for n, s, _ in requests(bench)][-1] == ("2", "beta")


def test_late_upgrade_never_asks_a_copy_the_filters_refuse() -> None:
    """`prefer_rank`: the better rank lists the chapter late, but in a language the series does
    not take: it is not asked as an upgrade."""
    with filter_bench({"/series-alpha": lang_of("en"), "/series-beta": lang_of("fr")}) as bench:
        with bench.lib.engine.begin() as connection:
            connection.execute(sa.text("UPDATE series_follow SET upgrade_policy = 'prefer_rank'"))
        set_filters(bench, {"languages": ["fr"]})
        serve(bench, BETA, listing(BETA, [1]))
        settle(bench)
        assert [(n, s) for n, s, _ in requests(bench)] == [("1", "beta")]
        serve(bench, ALPHA, listing(ALPHA, [1]))  # the better rank, in en
        settle(bench)
        assert [(n, s) for n, s, _ in requests(bench)] == [("1", "beta")]
        assert len(asked(bench)) == 1


def test_filtered_unit_is_never_asked_again_by_reopen() -> None:
    """A ticket given up before a filter was set is reopened by a healthy source: the filter is
    consulted before it is sent, and the Unit stays out."""
    with filter_bench(two_bindings=False) as bench:
        serve(bench, ALPHA, listing(ALPHA, [1]))
        bench.run()
        before = asked(bench)
        assert before
        with bench.lib.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE download_request SET state='abandoned', attempts=99, ticket=NULL")
            )
        set_filters(bench, {"languages": ["de"]})  # chapter 1 is not German: refused from now on
        bench.cycle._tickets.reopen_abandoned()
        bench.run()
        assert asked(bench) == before
        assert not bench.rows(
            "SELECT id FROM download_request WHERE state IN ('requested','downloaded')"
        )
