"""ENF-PERF-005: the local search answers in under 200 ms (p95) on the reference profile.

The measure is the real one: the CI profile (2 000 Works, 60 000 Units) is searched through the
real route (`GET /api/v1/search?q=`) by an authenticated administrator, so every word is resolved
by the FTS5, trigram and LIKE tables and filtered by the reader scope. The words are those of the
generated titles (a word shared by a tenth of the library, a pair, a prefix, another word, then a
single title by its number), and the answers are checked to hold hits: an empty answer is fast
and proves nothing. The threshold is the spec's, read from `.github/bench-reference.json`, and is
never loosened here.

Out of the required CI: `nightly.yml` (`bench-perf`), see `tools/traceability-exceptions.toml`.
"""

import pytest

from tests.bench.generator import _WORDS
from tests.bench.harness import p95
from tests.bench.perf_env import SAMPLES, Reference, budgets, record, timed, warm

pytestmark = pytest.mark.nightly


def queries() -> list[str]:
    words = list(_WORDS)
    forms: list[str] = []
    for n in range(SAMPLES):
        a, b = words[n % len(words)], words[(n * 3 + 1) % len(words)]
        forms.append((a, f"{a}+{b}", a[:3], b, f"{(n * 53) % 2_000:04d}")[n % 5])
    return [f"/api/v1/search?q={q}&limit=50" for q in forms]


@pytest.mark.req("ENF-PERF-005")
def test_bench_search_p95_under_200ms(reference: Reference) -> None:
    assert (reference.manifest.works, reference.manifest.units) == (2_000, 60_000)
    asked = queries()
    for path in asked[:5]:  # the five forms: a word, a pair, a prefix, another word, a number
        body = reference.client.get(path).json()
        assert body["total"] > 0, f"{path} found nothing on the reference profile"
    warm(reference.client, asked)
    samples = timed(reference.client, asked)
    got = p95(samples) * 1000
    record("search", p95_ms=got, budget_ms=budgets()["search_p95_ms"], samples=len(samples))
    assert got < budgets()["search_p95_ms"], (
        f"search p95 {got:.1f} ms over {len(samples)} queries "
        f"(budget {budgets()['search_p95_ms']} ms); slowest {max(samples) * 1000:.1f} ms"
    )
