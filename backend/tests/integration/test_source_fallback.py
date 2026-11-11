"""EF-SRC-037 / EF-INT-006: a dead binding falls back to the next rank, told once; the return to
the original rank is a setting, and never undoes what the admin disabled."""

from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.fallback import Fallback
from shelfhold.acquisition.application.tickets import DownloadTickets
from shelfhold.infrastructure.schema import acquisition
from tests.follow_env import Bench, follow_bench
from tests.integration.test_health_requeue import SOURCE_ALPHA, _drop_source
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)
from tests.library_env import lib_env
from tests.multi_source_env import ALPHA, BETA, bind_beta, chapters, requests, serve, watermark

pytestmark = pytest.mark.req("EF-SRC-037")
NOW = datetime(2026, 11, 11, tzinfo=UTC)


class _Told:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, connection: sa.Connection, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


def _state(connection: sa.Connection, binding_id: str) -> tuple[bool, str | None]:
    row = connection.execute(
        sa.select(acquisition.source_binding.c.active, acquisition.source_binding.c.disabled_reason)
        .where(acquisition.source_binding.c.id == binding_id)
    ).one()  # fmt: skip
    return bool(row.active), row.disabled_reason


def test_fall_back_postpones_open_requests_and_tells_once() -> None:
    with lib_env() as lib:
        told = _Told()
        fallback = Fallback(told)
        with lib.engine.begin() as connection:
            work = add_work(connection)
            edition = add_edition(connection, work)
            first = add_binding(connection, work, edition, add_source_item(connection, "a"))
            second = add_binding(
                connection, work, edition, add_source_item(connection, "b"), priority=2
            )
            unit = add_unit(connection, edition, "3")
            variant = add_variant(connection, unit, first, "a-3")
            add_variant(connection, unit, second, "b-3")
            DownloadTickets.open(
                connection, binding_id=first, variant_id=variant, unit_id=unit, now=NOW
            )
        with lib.engine.begin() as connection:
            assert fallback.fall_back(connection, first, "not_loaded") is True
            assert fallback.fall_back(connection, first, "not_loaded") is False  # not twice
        with lib.engine.connect() as connection:
            assert _state(connection, first) == (False, "not_loaded")
            assert _state(connection, second) == (True, None)
            states = connection.execute(sa.text("SELECT state FROM download_request")).scalars()
            assert list(states) == ["cancelled"]  # the next rank asks again, nothing waits
        ((kind, payload),) = told.events
        assert kind == "source.fallback"
        assert payload["from_binding_id"] == first and payload["to_binding_id"] == second
        assert payload["reason"] == "not_loaded"


def test_a_binding_alone_never_falls_back_and_another_work_is_never_a_target() -> None:
    with lib_env() as lib:
        told = _Told()
        with lib.engine.begin() as connection:
            work = add_work(connection)
            edition = add_edition(connection, work)
            only = add_binding(connection, work, edition, add_source_item(connection, "a"))
            other_work = add_work(connection)  # another Work, another Edition, rank 2 for its own
            add_binding(
                connection, other_work, add_edition(connection, other_work),
                add_source_item(connection, "z"), priority=2,
            )  # fmt: skip
            assert Fallback(told).fall_back(connection, only, "not_loaded") is False
        with lib.engine.connect() as connection:
            assert _state(connection, only) == (True, None)
        assert told.events == []


def test_dead_reason_waits_three_cycles_and_two_content_failures() -> None:
    with lib_env() as lib:
        fallback = Fallback(_Told())
        with lib.engine.begin() as connection:
            work = add_work(connection)
            edition = add_edition(connection, work)
            first = add_binding(connection, work, edition, add_source_item(connection, "a"))
            assert fallback.dead_reason(connection, first, not_loaded_cycles=2) is None
            assert fallback.dead_reason(connection, first, not_loaded_cycles=3) == "not_loaded"
            connection.execute(
                sa.insert(acquisition.source_failure).values(
                    source_binding_id=first,
                    limit_key="suwayomi:1",
                    failure_class="content",
                    consecutive_content_errors=1,
                )
            )
            assert fallback.dead_reason(connection, first, not_loaded_cycles=0) is None
            connection.execute(sa.update(acquisition.source_failure).values(
                consecutive_content_errors=2
            ))  # fmt: skip
            assert fallback.dead_reason(connection, first, not_loaded_cycles=0) == (
                "content_failures"
            )


def test_return_to_the_original_rank_is_a_setting_and_never_undoes_a_manual_disable() -> None:
    with lib_env() as lib:
        with lib.engine.begin() as connection:
            work = add_work(connection)
            edition = add_edition(connection, work)
            first = add_binding(connection, work, edition, add_source_item(connection, "a"))
            second = add_binding(
                connection, work, edition, add_source_item(connection, "b"), priority=2
            )
            manual = add_binding(
                connection, work, edition, add_source_item(connection, "c"), priority=3
            )
            Fallback(_Told()).fall_back(connection, first, "not_loaded")
            connection.execute(
                sa.update(acquisition.source_binding)
                .where(acquisition.source_binding.c.id == manual)
                .values(active=False, disabled_reason="manual")
            )
        with lib.engine.begin() as connection:
            assert Fallback(_Told(), return_to_origin=False).restore(connection, first) is False
            assert _state(connection, first) == (False, "not_loaded")  # the setting says stay
            assert Fallback(_Told()).restore(connection, manual) is False  # the admin's choice
            assert _state(connection, manual) == (False, "manual")
            assert Fallback(_Told()).restore(connection, first) is True
            assert _state(connection, first) == (True, None)
            assert _state(connection, second) == (True, None)


def test_unloaded_source_falls_back_after_three_cycles_and_comes_back() -> None:
    with follow_bench() as bench:
        beta = bind_beta(bench)
        watermark(bench)
        serve(bench, ALPHA, chapters(ALPHA, [1, 2]))
        serve(bench, BETA, chapters(BETA, [1, 2]))
        bench.run()
        gone = _drop_source(bench.server, SOURCE_ALPHA)  # the rank-1 extension is gone
        bench.run()
        bench.run()
        assert bench.binding()["active"] and bench.events("source.fallback") == []  # two cycles
        bench.run()  # the third
        assert bench.binding() == {"active": 0, "disabled_reason": "not_loaded"}
        (event,) = bench.events("source.fallback")
        assert event["to_binding_id"] == beta and event["reason"] == "not_loaded"
        bench.run()
        assert len(bench.events("source.fallback")) == 1  # told once
        bench.server.state["sources"].append(gone)  # the extension is back
        bench.run()
        assert bench.binding() == {"active": 1, "disabled_reason": None}  # the original rank


def test_unloaded_source_is_not_replaced_when_the_return_is_off() -> None:
    with follow_bench() as bench:
        bind_beta(bench)
        bench.cycle = bench.new_cycle(fallback_return=False)
        gone = _drop_source(bench.server, SOURCE_ALPHA)
        for _ in range(4):
            bench.run()
        assert bench.binding()["disabled_reason"] == "not_loaded"
        bench.server.state["sources"].append(gone)
        bench.run()
        assert bench.binding()["disabled_reason"] == "not_loaded"  # stays where it fell


def test_fallback_asks_the_next_rank_for_what_waited() -> None:
    with follow_bench() as bench:
        bind_beta(bench)
        watermark(bench)
        serve(bench, ALPHA, chapters(ALPHA, [1]))
        serve(bench, BETA, chapters(BETA, [1]))
        gone = _drop_source(bench.server, SOURCE_ALPHA)  # nothing is asked of rank 1 any more
        for _ in range(8):
            bench.run()
        assert [(n, s) for n, s, _ in requests(bench)] == [("1", "beta")]
        assert gone and isinstance(bench, Bench)


def test_the_return_to_the_original_rank_is_a_setting_on_by_default() -> None:
    from shelfhold.config import Settings

    assert Settings.model_fields["follow_fallback_return"].default is True


def test_limit_key_and_slots_of_the_service_are_what_the_cycle_uses() -> None:
    """EF-SRC-037: the request budget counts under the service's `limit_key`, and the slot count
    of each sub-source is declared from the service's `limit_slots`."""
    from shelfhold.jobs.public import DomainSlots

    with follow_bench() as bench:
        slots = DomainSlots(bench.lib.engine, bench.clock)
        bench.cycle = bench.new_cycle(slots=slots)
        asked: list[str] = []
        original = bench.service.limit_key_for

        def spy(ref: Any) -> str:
            asked.append(ref.source_key)
            return original(ref)

        bench.service.limit_key_for = spy  # type: ignore[method-assign]
        watermark(bench)
        serve(bench, ALPHA, chapters(ALPHA, [1]))
        bench.run()
        assert "suwayomi:7001" in asked
        declared = bench.rows("SELECT limit_key, slots FROM domain_slot")
        assert {r["limit_key"] for r in declared} >= {"suwayomi:7001", "suwayomi:7002"}
