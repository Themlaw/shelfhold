"""ENF-OPS-011 (M3): every dependency of the failure matrix, end to end.

An infrastructure failure pauses the queue concerned and never counts as a content failure; every
row has an injection, a waiting state, an event code and a UI action.
"""

import pytest

from shelfhold.events.operating import OPERATING_EVENTS
from shelfhold.i18n import load_catalog
from shelfhold.infrastructure.observability.failure_matrix import (
    FAILURE_MATRIX,
)
from shelfhold.interfaces.http_api.catalog import all_error_codes
from tests.support.faults import SCENARIOS


@pytest.mark.req("ENF-OPS-011")
def test_infra_failure_pauses_not_failures() -> None:
    """A failing dependency pauses the queue; no source/content failure is counted."""
    from tests.follow_env import follow_bench

    with follow_bench() as bench:
        bench.run()
        original = bench.service.followed_series

        async def down(fetcher: object) -> object:
            from shelfhold.sdk.source import TemporarilyUnavailable

            raise TemporarilyUnavailable("connection refused")

        bench.service.followed_series = down  # type: ignore[method-assign]
        for _ in range(2):
            assert not bench.run().unavailable
        report = bench.run()
        assert report.unavailable
        assert bench.rows("SELECT * FROM source_failure") == []
        assert bench.binding()["active"]

        bench.service.followed_series = original
        back = bench.run()
        assert not back.unavailable


@pytest.mark.req("ENF-OPS-011")
def test_failure_matrix_every_row_injected() -> None:
    """Every row of the matrix is either replayed here or proved by a named test."""
    for row in FAILURE_MATRIX:
        assert row.simulated or row.proof, f"{row.key}: neither injected nor proved"
        assert row.injection, f"{row.key}: no injection name"
    # the simulable rows are exactly the ones with an injection scenario
    assert {r.key for r in FAILURE_MATRIX if r.simulated} == set(SCENARIOS)
    for key in SCENARIOS:
        row = next(r for r in FAILURE_MATRIX if r.key == key)
        assert row.event in OPERATING_EVENTS


@pytest.mark.req("ENF-OPS-011")
def test_failure_matrix_rows_have_ui_action() -> None:
    """Every row carries an event code and a UI action (text in the catalog)."""
    catalog = load_catalog()
    codes = {c.code: c for c in all_error_codes()}
    for row in FAILURE_MATRIX:
        assert row.event in OPERATING_EVENTS, row.key
        assert row.event in codes, f"{row.key}: {row.event} is not an error code"
        entry = codes[row.event]
        assert catalog.get(entry.title_key), f"{row.key}: no title text"
        assert catalog.get(entry.action_key), f"{row.key}: no action text"
        assert row.ui_action, f"{row.key}: no ui action"
        if row.needs_human:
            assert row.exit_command, f"{row.key}: a human must act, so a command must exist"
