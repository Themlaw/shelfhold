"""ENF-PERF-004, -005, -007: the p95 of the API benches is a real nearest-rank percentile, so a slow
tail is never averaged away and a bench cannot pass on a smoothed number."""

import pytest

from tests.bench.harness import load_reference, p95, percentile


@pytest.mark.req("ENF-PERF-004")
def test_p95_is_the_nearest_rank_not_an_average() -> None:
    samples = [float(n) for n in range(1, 101)]  # 1..100
    assert p95(samples) == 95.0
    assert p95([*samples, 1000.0]) == 96.0  # 101 samples: rank 97 of 101 is ceil(95.95) = 96


@pytest.mark.req("ENF-PERF-005")
def test_p95_sees_a_slow_tail_that_the_median_hides() -> None:
    samples = [0.01] * 90 + [0.9] * 10
    assert percentile(samples, 0.5) == 0.01
    assert p95(samples) == 0.9


@pytest.mark.req("ENF-PERF-007")
def test_p95_refuses_nothing_to_measure_and_a_wrong_fraction() -> None:
    with pytest.raises(ValueError, match="no sample"):
        p95([])
    for bad in (0, -0.1, 1.5):
        with pytest.raises(ValueError, match="fraction"):
            percentile([1.0], bad)


@pytest.mark.req("ENF-PERF-004", "ENF-PERF-005", "ENF-PERF-007", "ENF-PERF-010")
def test_reference_budgets_are_the_specs_and_never_loosened() -> None:
    """The numbers the benches read are the spec's (§7): a bench cannot be made to pass by
    editing a threshold without this test, and so a review, noticing."""
    api = load_reference()["budgets"]["api"]
    assert api["list_p95_ms"] == 300  # ENF-PERF-004
    assert api["search_p95_ms"] == 200  # ENF-PERF-005
    assert (api["contention_max_factor"], api["export_chapters"]) == (2, 500)  # ENF-PERF-007
    assert (api["screen_covers"], api["screen_max_bytes"]) == (60, int(1.5 * 1024 * 1024))  # -010
