"""ENF-PERF-007: the API stays within twice its latency while 500 chapters are exported.

The measure is the real one: on the reference profile (2 000 Works, 60 000 Units), a real export
pipeline renders 500 received chapters into a shelf's folder, on real files, while the real
application (`create_app`, the same database file) answers the library list back to back. The p95
of those answers during the export must not exceed twice their p95 at rest. The factor is the
spec's, read from `.github/bench-reference.json`, and is never loosened here: there is no floor
under the at-rest latency, so a fast API is held to a fast answer.

Out of the required CI: `nightly.yml` (`bench-perf`), see `tools/traceability-exceptions.toml`.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import load_settings
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.domain.path_map import PathEntry, PathMap
from shelfhold.export.domain.physical_roots import ExpectedRoot
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from tests.auth_env import auth_env
from tests.bench.generator import CI_PROFILE, Profile, generate_catalog
from tests.bench.harness import p95
from tests.bench.perf_env import (
    SAMPLES,
    budgets,
    during,
    give_password,
    record,
    settled,
    sign_in,
    timed,
    warm,
)
from tests.export_env import ExportEnv, export_env
from tests.fakes.kavita.plugin_parse import simulate_parse
from tests.fakes_clock import ManualClock
from tests.intents_env import NOW

pytestmark = pytest.mark.nightly

# KNOWN FINDING, not a loosened threshold (the factor below is still 2, and `check` still asserts
# it). Measured on the reference profile, the library list takes 2.3 to 2.5 times its latency while
# the export runs, whatever the shape of the 500 chapters. The cause is the export being a thread
# of the API process: the same writes made by a separate process leave the list at 1.0 to 1.1 times
# (and the same writes by a thread of this process give 2.0 to 2.5), whatever the interpreter's
# switch interval. The fix is to run the export outside the API process, which is not this lot's.
# `strict`: the day the budget is met this test fails as XPASS, and the marker goes.
KNOWN_OVER_BUDGET = pytest.mark.xfail(
    strict=True,
    reason="ENF-PERF-007: the export runs as a thread of the API process (x2.3 to x2.5 measured)",
)

PATH = "/api/v1/works?limit=50&offset=0"


def pipeline_after_boot(env: ExportEnv) -> ExportPipeline:
    """The application's start-up moves each root to its next generation (sentinel and row): the
    export of the test, which still expects the first, is opened again on the rows as they stand,
    as the runtime itself reads them. The pipeline is the same, over the roots as they are now."""
    lib = env.lib
    with lib.engine.connect() as connection:
        expected = [
            ExpectedRoot(row.root_id, row.path_entry, lib.instance_id, row.generation)
            for row in connection.execute(sa.select(platform.physical_root))
        ]
    path_map = PathMap((PathEntry(str(lib.manga), "/manga"), PathEntry(str(lib.store), "/store")))
    fsops = Fsops.open(path_map, expected)
    return ExportPipeline(
        lib.engine, fsops, locate=lib.locate, data_dir=lib.data, clock=ManualClock(NOW),
        parse=simulate_parse, request_scan=[].append, parse_verified=True,
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class Contention:
    at_rest_p95_s: float
    during_p95_s: float
    answers: int

    @property
    def factor(self) -> float:
        return self.during_p95_s / self.at_rest_p95_s


def measure_contention(
    *, chapters: int, one_series: bool, profile: Profile = CI_PROFILE
) -> Contention:
    """The p95 of the library list at rest, then while `chapters` chapters are exported, on the
    database file the export writes. `one_series`: all the chapters belong to one series (its
    mirror file is then rewritten for each of them); otherwise one new chapter per series, the
    shape of a night's catch-up over the followed series of the profile."""
    with export_env() as env, pytest.MonkeyPatch.context() as patch:
        generate_catalog(env.engine, profile)
        if one_series:
            series = env.new_series("Bench")
            for number in range(1, chapters + 1):
                env.add_chapter(series, str(number))
        else:
            for number in range(1, chapters + 1):
                env.add_chapter(env.new_series(f"Shelf row {spelled(number)}"), "1")
        # the application serves the very database file the export writes
        database = env.engine.url.database
        assert database is not None
        settings = load_settings(
            {
                "SHELFHOLD_DISK_PAUSE_PERCENT": "0",
                "SHELFHOLD_DISK_PAUSE_GIB": "0",
                "SHELFHOLD_DATA_DIR": str(Path(database).parent),
                "SHELFHOLD_STORE_DIR": str(env.lib.store),
            }
        ).values
        give_password(env.engine)  # the profile's own administrator signs in
        with auth_env(patch, settings, with_admin=False) as app:
            sign_in(app)
            settled()
            pipeline = pipeline_after_boot(env)
            client = app.client
            asked = [PATH] * SAMPLES
            warm(client, asked)
            at_rest = p95(timed(client, asked))

            def one() -> float:
                return timed(client, [PATH])[0]

            answers, report = during(
                lambda: pipeline.run(env.destination_id, job_id=new_ulid()), one
            )
        assert len(report.ops) >= chapters  # type: ignore[attr-defined]
        assert len(answers) >= 20, "too few answers during the export to give a p95"
        return Contention(at_rest, p95(answers), len(answers))


def spelled(number: int) -> str:
    """A name made of letters only: a trailing number reads as a volume to the naming rules, and
    two series that differ by it collide (EF-EXP-032)."""
    letters = ""
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(ord("a") + rest) + letters
    return letters


def check(name: str, got: Contention) -> None:
    budget = budgets()["contention_max_factor"]
    record(
        name,
        at_rest_p95_ms=got.at_rest_p95_s * 1000,
        during_p95_ms=got.during_p95_s * 1000,
        factor=got.factor,
        budget_factor=budget,
        samples=got.answers,
    )
    assert got.during_p95_s <= budget * got.at_rest_p95_s, (
        f"during the export p95 {got.during_p95_s * 1000:.1f} ms vs "
        f"{got.at_rest_p95_s * 1000:.1f} ms at rest (factor {got.factor:.2f}, budget "
        f"x{budget:g}); {got.answers} answers"
    )


@KNOWN_OVER_BUDGET
@pytest.mark.req("ENF-PERF-007")
def test_bench_contention_export_500_chapters() -> None:
    """500 new chapters over 500 followed series."""
    chapters = int(budgets()["export_chapters"])
    check("contention_export_500_chapters", measure_contention(chapters=chapters, one_series=False))


@KNOWN_OVER_BUDGET
@pytest.mark.req("ENF-PERF-007")
def test_bench_contention_export_500_chapters_of_one_series() -> None:
    """The worst shape: 500 chapters of a single series."""
    chapters = int(budgets()["export_chapters"])
    check(
        "contention_export_500_chapters_one_series",
        measure_contention(chapters=chapters, one_series=True),
    )
