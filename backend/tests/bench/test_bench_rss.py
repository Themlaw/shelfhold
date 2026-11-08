"""ENF-PERF-001 (culpable): the server holds under 250 MB of resident memory at rest, on the
reference profile (2 000 Works, 60 000 Units), workers included.

The measure is the real one: the `VmRSS` of the `shelfhold serve` process and of every process
below it, read from `/proc` (`tests/bench/rss.py`), after the server has been ready for a while.
`MALLOC_ARENA_MAX=2` is set as in the image. The reference profile is the CI one (sparse files
are not even needed for a metadata-only server): same catalog as the full profile.
The thresholds come from the versioned reference (`.github/bench-reference.json`) and are the
spec's, never loosened here.

The heavy bench of the peak (500 chapters) is `tests/nightly/test_bench_rss_heavy.py`; the PR
keeps `test_bench_peak_rss_light_export` (a few dozen chapters), well under ten minutes.
"""

import pytest

from shelfhold.config import Settings
from tests.bench.generator import CI_PROFILE, generate_catalog
from tests.bench.harness import export_peak, load_reference, running_server
from tests.bench.rss import MIB, rss_tree


@pytest.mark.req("ENF-PERF-001")
def test_bench_idle_rss_under_250mb(settings: Settings) -> None:
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.migrate import migrate

    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        manifest = generate_catalog(engine, CI_PROFILE)
    finally:
        engine.dispose()
    assert (manifest.works, manifest.units) == (2_000, 60_000)
    budget = load_reference()["budgets"]["idle_rss_mb"]
    with running_server(settings, settle_s=3.0) as server:
        rss = rss_tree(server.pid)
    assert rss.processes >= 1
    assert rss.total_bytes < budget * MIB, f"{rss.total_bytes / MIB:.1f} MB over {rss.processes}"


@pytest.mark.req("ENF-PERF-009")
def test_bench_peak_rss_light_export() -> None:
    """The bench kept in the PR: the same measure as the nightly one, on 50 chapters. The peak
    of 500 chapters over the whole profile is `tests/nightly/test_bench_rss_heavy.py`."""
    peak = export_peak(chapters=50, decodes=5, profile=False)
    assert peak.ops >= 50  # the export really wrote its chapters
    assert peak.tree_bytes < load_reference()["budgets"]["peak_rss_mb"] * MIB
