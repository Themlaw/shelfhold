"""ENF-PERF-009 (culpable), nightly: the resident memory peak stays under 500 MB while 500 chapters
are exported, on the whole reference profile, decode workers included.

Why it is not in the pull-request CI: it builds 60 000 Units of catalog, ingests and renders 500
archives and spawns decode workers, then samples `/proc` for the length of the export, on a runner
that has to be quiet for the number to mean something. It runs in `nightly.yml` (`bench-rss`, also
by hand) and is listed in tools/traceability-exceptions.toml; the pull request keeps the 50-chapter
variant of `tests/bench/test_bench_rss.py`.
"""

import pytest

from tests.bench.harness import export_peak, load_reference
from tests.bench.rss import MIB

pytestmark = pytest.mark.nightly


@pytest.mark.req("ENF-PERF-009")
def test_bench_peak_rss_under_500mb_export_500_chapters() -> None:
    peak = export_peak(chapters=500, decodes=50, profile=True)
    assert peak.ops >= 500
    budget = load_reference()["budgets"]["peak_rss_mb"] * MIB
    assert peak.tree_bytes < budget, f"{peak.tree_bytes / MIB:.1f} MB over {peak.processes} procs"
    assert peak.own_high_water_bytes < budget
