"""EF-SRC-037: a followed Edition with ranked bindings. One request per Unit, the best rank that
has the Unit, the team before the rank, a notified fallback, the late upgrade by policy."""

import threading
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.tickets import DownloadTickets
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog
from tests.follow_env import Bench, follow_bench
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)
from tests.library_env import lib_env
from tests.multi_source_env import (
    ALPHA,
    BETA,
    bind_beta,
    chapters,
    policy,
    requests,
    serve,
    settle,
    watermark,
)

pytestmark = pytest.mark.req("EF-SRC-037")


def test_follow_picks_best_ranked_binding_that_has_unit() -> None:
    with follow_bench() as bench:
        bind_beta(bench)
        watermark(bench)
        serve(bench, ALPHA, chapters(ALPHA, [1, 2]))
        serve(bench, BETA, chapters(BETA, [1, 2, 3]))  # chapter 3 is published at rank 2 only
        bench.run()
        assert [(n, series) for n, series, _ in requests(bench)] == [
            ("1", "alpha"), ("2", "alpha"), ("3", "beta"),
        ]  # fmt: skip


def test_unit_never_requested_from_two_bindings_same_cycle() -> None:
    with follow_bench() as bench:
        bind_beta(bench)
        watermark(bench)
        serve(bench, ALPHA, chapters(ALPHA, [1, 2, 3]))
        serve(bench, BETA, chapters(BETA, [1, 2, 3]))
        first = bench.run()
        assert first.requested == 3
        assert [(n, series) for n, series, _ in requests(bench)] == [
            ("1", "alpha"), ("2", "alpha"), ("3", "alpha"),
        ]  # fmt: skip
        settle(bench)
        assert len(requests(bench)) == 3  # nor in the cycles that follow


def test_unit_never_requested_when_the_better_binding_is_ingested_last() -> None:
    """The listing order of a cycle is the rank order, but a Variant that arrives second must
    never open a second ticket for a Unit that already has one."""
    with follow_bench() as bench:
        with bench.lib.engine.begin() as connection:
            connection.execute(
                sa.update(acquisition.source_binding)
                .where(acquisition.source_binding.c.id == bench.binding_id)
                .values(priority=9)
            )
        bind_beta(bench, priority=1)  # Beta is the better rank: Alpha is the one ingested last
        watermark(bench)
        serve(bench, ALPHA, chapters(ALPHA, [1, 2]))
        serve(bench, BETA, chapters(BETA, [1, 2]))
        bench.run()
        assert [(n, series) for n, series, _ in requests(bench)] == [
            ("1", "beta"), ("2", "beta"),
        ]  # fmt: skip


def test_team_preference_beats_rank() -> None:
    with follow_bench() as bench:
        bind_beta(bench)
        serve(bench, ALPHA, chapters(ALPHA, [1], "A"))
        serve(bench, BETA, chapters(BETA, [1], "B"))
        bench.run()  # no watermark yet: the Variants and the teams are known, nothing is asked
        with bench.lib.engine.begin() as connection:
            team_b = connection.execute(
                sa.select(catalog.team.c.id).where(catalog.team.c.label == "team b")
            ).scalar_one()
            connection.execute(
                sa.insert(catalog.team_priority).values(
                    edition_id=bench.edition_id, team_id=team_b, rank=1
                )
            )
        watermark(bench)
        bench.run()
        assert [(n, series) for n, series, _ in requests(bench)] == [("1", "beta")]


def _two_workers_env() -> Any:
    return lib_env()


def test_two_workers_never_open_two_requests_for_one_unit() -> None:
    """The database says no, whatever the writers: two Variants of a Unit, two connections."""
    with lib_env() as lib:
        with lib.engine.begin() as connection:
            work_id = add_work(connection)
            edition_id = add_edition(connection, work_id)
            first = add_binding(connection, work_id, edition_id, add_source_item(connection, "a"))
            second = add_binding(
                connection, work_id, edition_id, add_source_item(connection, "b"), priority=2
            )
            unit_id = add_unit(connection, edition_id, "7")
            variants = [
                (first, add_variant(connection, unit_id, first, "a-7")),
                (second, add_variant(connection, unit_id, second, "b-7")),
            ]
        opened: list[bool] = []
        barrier = threading.Barrier(2)

        def worker(binding_id: str, variant_id: str) -> None:
            barrier.wait()
            with lib.engine.begin() as connection:
                opened.append(
                    DownloadTickets.open(
                        connection, binding_id=binding_id, variant_id=variant_id,
                        unit_id=unit_id, now=_now(),
                    )
                )  # fmt: skip

        threads = [threading.Thread(target=worker, args=pair) for pair in variants]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(opened) == [False, True]
        with lib.engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT count(*) FROM download_request WHERE state = 'requested'")
            ).scalar_one() == 1

        with pytest.raises(sa.exc.IntegrityError), lib.engine.begin() as connection:
            connection.execute(  # a writer that skips `open` meets the same wall
                sa.insert(acquisition.download_request).values(
                    id=new_ulid(), target_kind="unit", variant_id=variants[1][1],
                    source_binding_id=variants[1][0], unit_id=unit_id, state="requested",
                )
            )  # fmt: skip


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 11, 11, tzinfo=UTC)


def test_a_binding_of_another_work_never_attaches_to_the_edition() -> None:
    with lib_env() as lib, lib.engine.begin() as connection:
        mine = add_edition(connection, add_work(connection))
        other_work = add_work(connection)
        item = add_source_item(connection, "x")
        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            connection.execute(  # the Edition is mine, the Work is not
                sa.insert(acquisition.source_binding).values(
                    id=new_ulid(), work_id=other_work, edition_id=mine, source_item_id=item,
                    priority=1, active=True,
                )
            )  # fmt: skip


def _fell_back(bench: Bench) -> list[dict[str, Any]]:
    return bench.events("source.fallback")


def test_dead_binding_falls_back_with_notification() -> None:
    with follow_bench() as bench:
        beta_binding = bind_beta(bench)
        watermark(bench)
        serve(bench, ALPHA, chapters(ALPHA, [1, 2]))
        serve(bench, BETA, chapters(BETA, [1, 2]))
        settle(bench)
        bench.alpha()["inLibrary"] = False  # the rank-1 series leaves the library...
        bench.run()
        bench.run()  # ...for two cycles: removed upstream
        assert bench.binding() == {"active": 0, "disabled_reason": "removed_upstream"}
        (event,) = _fell_back(bench)  # told, once
        assert event["edition_id"] == bench.edition_id
        assert event["from_binding_id"] == bench.binding_id
        assert event["to_binding_id"] == beta_binding
        serve(bench, ALPHA, chapters(ALPHA, [1, 2]))
        serve(bench, BETA, chapters(BETA, [1, 2, 3]))
        bench.run()
        settle(bench)
        assert len(_fell_back(bench)) == 1  # never twice for the same episode
        assert [(n, s) for n, s, _ in requests(bench)][-1] == ("3", "beta")  # the next rank asks


def test_no_fallback_is_told_when_no_other_binding_can_take_over() -> None:
    with follow_bench() as bench:
        serve(bench, ALPHA, chapters(ALPHA, [1]))
        bench.run()
        bench.alpha()["inLibrary"] = False
        bench.run()
        bench.run()
        assert bench.binding()["disabled_reason"] == "removed_upstream"
        assert _fell_back(bench) == []  # nothing to fall back to: the removal is told alone
        assert len(bench.events("series.removed_upstream")) == 1


def test_binding_of_another_edition_is_never_a_fallback_target() -> None:
    with follow_bench() as bench:
        bench.add_binding("suwayomi:7002", "/series-beta")  # another Edition, another Work
        serve(bench, ALPHA, chapters(ALPHA, [1]))
        bench.run()
        bench.alpha()["inLibrary"] = False
        bench.run()
        bench.run()
        assert _fell_back(bench) == []


def _held_from_beta(bench: Bench) -> None:
    """Chapter 1 is on disk, taken from the rank-2 binding: the rank-1 series did not list it."""
    watermark(bench)
    serve(bench, ALPHA, [])
    serve(bench, BETA, chapters(BETA, [1]))
    settle(bench)
    assert [(n, s, st) for n, s, st in requests(bench)] == [("1", "beta", "fetched")]


class _Upgrader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def upgrade_unit(self, unit_id: str, content_id: str) -> list[str]:
        self.calls.append((unit_id, content_id))
        return ["upgraded"]


def _with_upgrader(bench: Bench) -> _Upgrader:
    upgrader = _Upgrader()
    bench.cycle = bench.new_cycle(upgrader=upgrader)  # type: ignore[call-arg]
    return upgrader


def test_late_better_rank_upgrade_follows_policy() -> None:
    with follow_bench() as bench:
        bind_beta(bench)
        upgrader = _with_upgrader(bench)
        _held_from_beta(bench)
        policy(bench, "prefer_rank")
        serve(bench, ALPHA, chapters(ALPHA, [1]))  # the better rank publishes it late
        settle(bench)
        assert [(n, s) for n, s, _ in requests(bench)] == [("1", "beta"), ("1", "alpha")]
        ((unit_id, content_id),) = upgrader.calls
        (row,) = bench.rows(
            "SELECT c.unit_id AS u, si.remote_id AS r FROM content c "
            "JOIN unit_variant v ON v.id = c.variant_id "
            "JOIN source_binding b ON b.id = v.source_binding_id "
            "JOIN source_item si ON si.id = b.source_item_id WHERE c.id = :c",
            c=content_id,
        )
        assert row == {"u": unit_id, "r": "/series-alpha"}
        settle(bench)
        assert len(upgrader.calls) == 1  # once


def test_late_better_rank_arrival_changes_nothing_under_keep_first() -> None:
    with follow_bench() as bench:
        bind_beta(bench)
        upgrader = _with_upgrader(bench)
        _held_from_beta(bench)  # the default policy is keep_first
        serve(bench, ALPHA, chapters(ALPHA, [1]))
        settle(bench)
        assert [(n, s) for n, s, _ in requests(bench)] == [("1", "beta")]
        assert upgrader.calls == []


def test_a_late_arrival_of_a_worse_rank_never_upgrades() -> None:
    with follow_bench() as bench:
        bind_beta(bench)
        upgrader = _with_upgrader(bench)
        policy(bench, "prefer_rank")
        watermark(bench)
        serve(bench, ALPHA, chapters(ALPHA, [1]))
        serve(bench, BETA, [])
        settle(bench)
        serve(bench, BETA, chapters(BETA, [1]))  # rank 2 arrives after rank 1 is held
        settle(bench)
        assert [(n, s) for n, s, _ in requests(bench)] == [("1", "alpha")]
        assert upgrader.calls == []


def test_a_late_better_rank_of_another_team_never_replaces_the_held_translation() -> None:
    with follow_bench() as bench:
        bind_beta(bench)
        upgrader = _with_upgrader(bench)
        policy(bench, "prefer_rank")
        _held_from_beta(bench)
        serve(bench, ALPHA, chapters(ALPHA, [1], "B"))  # rank 1, but another team
        settle(bench)
        assert [(n, s) for n, s, _ in requests(bench)] == [("1", "beta")]
        assert upgrader.calls == []


def test_a_variant_tied_to_a_binding_of_another_edition_is_never_an_upgrade() -> None:
    with follow_bench() as bench:
        bind_beta(bench)
        upgrader = _with_upgrader(bench)
        policy(bench, "prefer_rank")
        _held_from_beta(bench)
        foreign = bench.add_binding("suwayomi:7003", "/series-gamma")  # another Work, rank 1
        with bench.lib.engine.begin() as connection:
            unit_id = connection.execute(sa.select(catalog.unit.c.id)).scalars().first()
            add_variant(connection, str(unit_id), foreign, "stray-1")
        settle(bench)
        assert [(n, s) for n, s, _ in requests(bench)] == [("1", "beta")]
        assert upgrader.calls == []
