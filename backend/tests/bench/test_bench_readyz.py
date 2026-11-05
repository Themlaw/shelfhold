"""ENF-PERF-003 (culpable): `/readyz` answers 200 in under 15 s after the process starts,
migration time excluded.

The spec's name is `bench_readyz_under_15s`; pytest only collects `test_*`, so the function carries
the `test_` prefix and the spec name lives in this docstring (PLAN-M1a A11). The threshold comes
from the spec and is never loosened here. The time is the median of `RUNS` cold starts: a single
slow run on a busy runner does not fail the job, a slow start does. Synthetic data, no network.
"""

import pytest

from shelfhold.config import Settings
from tests.bench.harness import (
    READYZ_BUDGET_S,
    RUNS,
    median_seconds,
    seed_reference_catalog,
    time_cold_start_to_ready,
)


@pytest.mark.req("ENF-PERF-003")
def test_bench_readyz_under_15s(settings: Settings) -> None:
    """bench_readyz_under_15s"""
    from shelfhold.infrastructure.migrate import migrate

    migrate(settings)  # excluded from the measure: ENF-PERF-003 says "outside migration time"
    seed_reference_catalog(settings)
    samples = [time_cold_start_to_ready(settings) for _ in range(RUNS)]
    median = median_seconds(samples)
    assert median < READYZ_BUDGET_S, f"median {median:.2f}s over {RUNS} runs: {samples}"


@pytest.mark.req("ENF-PERF-003")
def test_median_is_robust_to_one_slow_sample() -> None:
    assert median_seconds([1.0, 1.2, 90.0, 1.1, 1.3]) == pytest.approx(1.2)
    with pytest.raises(ValueError, match="no sample"):
        median_seconds([])
