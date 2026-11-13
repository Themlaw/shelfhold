"""ENF-PERF-004: the library list answers in under 300 ms (p95) on the reference profile.

The measure is the real one: the CI profile (2 000 Works, 60 000 Units) is served by the real
application, through the real route (`GET /api/v1/works`), to an authenticated administrator, so
the reader scope, the visibility filter, the total and the title order all run. Pages are walked
(first, middle, last), not one page asked a hundred times. The threshold is the spec's, read from
`.github/bench-reference.json`, and is never loosened here.

Out of the required CI: `nightly.yml` (`bench-perf`), see `tools/traceability-exceptions.toml`.
"""

import pytest

from tests.bench.harness import p95
from tests.bench.perf_env import SAMPLES, Reference, budgets, record, timed, warm

pytestmark = pytest.mark.nightly

PAGE = 50


def paths(total: int) -> list[str]:
    last = (total - 1) // PAGE * PAGE
    offsets = [(n * 7 * PAGE) % (last + PAGE) for n in range(SAMPLES)]
    return [f"/api/v1/works?limit={PAGE}&offset={o}" for o in offsets]


@pytest.mark.req("ENF-PERF-004")
def test_bench_library_list_p95_under_300ms(reference: Reference) -> None:
    assert (reference.manifest.works, reference.manifest.units) == (2_000, 60_000)
    asked = paths(reference.manifest.works)
    first = reference.client.get(asked[0]).json()
    assert first["total"] == reference.manifest.works  # the whole library is in the answer
    assert len(first["items"]) == PAGE
    warm(reference.client, asked)
    samples = timed(reference.client, asked)
    got = p95(samples) * 1000
    record("library_list", p95_ms=got, budget_ms=budgets()["list_p95_ms"], samples=len(samples))
    assert got < budgets()["list_p95_ms"], (
        f"library list p95 {got:.1f} ms over {len(samples)} pages "
        f"(budget {budgets()['list_p95_ms']} ms); slowest {max(samples) * 1000:.1f} ms"
    )
