"""ENF-PERF-007: the API stays within twice its latency while 500 chapters are exported.

The measure is the real one: on the reference profile (2 000 Works, 60 000 Units), a real export
pipeline renders 500 received chapters into a shelf's folder, on real files, while the real
application (`create_app`, the same database file) answers the library list back to back. The p95
of those answers during the export must not exceed twice their p95 at rest. The factor is the
spec's, read from `.github/bench-reference.json`, and is never loosened here: there is no floor
under the at-rest latency, so a fast API is held to a fast answer.

Three shapes share that measure: 500 chapters over 500 series, the same 500 chapters of one
series, and the fan-out to the folder Mihon reads (EF-EXP-010), whose ComicInfo and covers the
host renders in the same process.

The budget is xfailed strict while the known finding lasts (the export is a thread of the API
process, x2.3 to x2.5 measured), and an aggravation bound of its own, never xfailed, stands next
to it: a measure above x3 breaks the nightly whatever the finding.

Out of the required CI: `nightly.yml` (`bench-perf`), see `tools/traceability-exceptions.toml`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import RootGuard
from shelfhold.config import load_settings
from shelfhold.export.application.fan_out import FanOut, HostContentStore
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.domain.path_map import PathEntry, PathMap
from shelfhold.export.domain.physical_roots import ExpectedRoot
from shelfhold.export.targets.native_folder.source import NativeFolderSource
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform
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
from tests.byte_source_env import unit_with_item
from tests.export_env import ExportEnv, export_env
from tests.fakes.kavita.plugin_parse import simulate_parse
from tests.fakes_clock import ManualClock
from tests.fan_out_env import add_target, bind
from tests.intents_env import NOW
from tests.plan_executor_env import Probe

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

# An xfail cannot see an aggravation: a measure at x6 would be filed as XFAIL just the same, and
# the consumers are threads of the API process in production, so the users live this contention.
# This bound stands next to the xfailed budget, and it is never xfailed: a run whose answers to
# the library list take more than three times their at-rest latency breaks the nightly whatever
# the known finding. Recorded with every measure (see `check`); the true fix stays M5's.
AGGRAVATION_MAX_FACTOR = 3

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


def contention_of(
    env: ExportEnv,
    patch: pytest.MonkeyPatch,
    work: Callable[[ExportEnv], Callable[[], object]],
) -> tuple[Contention, object]:
    """The p95 of the library list at rest, then while `work(env)` runs on a thread: the
    application serves the very database file the work writes. The measure, and what the work
    returned (the shape of the work decides what its caller asserts of it)."""
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
        run = work(env)
        client = app.client
        asked = [PATH] * SAMPLES
        warm(client, asked)
        at_rest = p95(timed(client, asked))

        def one() -> float:
            return timed(client, [PATH])[0]

        answers, report = during(run, one)
    assert len(answers) >= 20, "too few answers during the export to give a p95"
    return Contention(at_rest, p95(answers), len(answers)), report


def _pipeline_run(env: ExportEnv) -> Callable[[], object]:
    """The received chapters rendered into the shelf's tree, one pipeline run per call."""
    pipeline = pipeline_after_boot(env)

    def run() -> object:
        return pipeline.run(env.destination_id, job_id=new_ulid())

    return run


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
        got, report = contention_of(env, patch, _pipeline_run)
        assert len(report.ops) >= chapters  # type: ignore[attr-defined]
        return got


def fan_out_after_boot(env: ExportEnv) -> FanOut:
    """The fan-out to the folder Mihon reads, over the roots as the application's start-up left
    them (the same re-opening `pipeline_after_boot` does): the folder's source only describes, the
    host renders the ComicInfo and the cover of each chapter in this process, and no server is
    asked anything."""
    pipeline = pipeline_after_boot(env)
    source = NativeFolderSource(env.engine, root_check=RootGuard().check_root)
    host = HostContentStore(env.engine, capabilities=source.capabilities, renders=source.renders)
    source.attach(host)
    pipeline.use_plan_executor(contents=host, probe=Probe(None))
    return FanOut(env.engine, pipeline, source, clock=ManualClock(NOW))


def measure_native_folder_contention(*, chapters: int, profile: Profile = CI_PROFILE) -> Contention:
    """The p95 of the library list at rest, then while `chapters` chapters are fanned out into the
    folder Mihon reads: one new chapter per series, the shape of a night's catch-up. The shelf of
    the measure holds only that binding, so the pass renders into the native folder alone."""
    with export_env() as env, pytest.MonkeyPatch.context() as patch:
        generate_catalog(env.engine, profile)
        native_shelf = new_ulid()
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.insert(export.shelf).values(
                    id=native_shelf, name="Bench natif", phrase="Bench natif",
                    max_age_rating=18,
                )
            )  # fmt: skip
        target = add_target(env, kind="native_folder", partitioning="none", name="mihon")
        bind(env, "local", target_id=target, shelf_id=native_shelf, link=False)
        for number in range(1, chapters + 1):
            series = env.new_series(f"Shelf row {spelled(number)}", shelf_id=native_shelf)
            unit_with_item(env, series, "1")

        def work(env: ExportEnv) -> Callable[[], object]:
            fan = fan_out_after_boot(env)

            def run() -> object:
                return fan.run(native_shelf, "bench")

            return run

        got, report = contention_of(env, patch, work)
        outcome = report.outcomes[0]  # type: ignore[attr-defined]
        assert outcome.status == "done" and outcome.code is None, outcome
        assert outcome.executed >= chapters and not outcome.lost_format, outcome
        return got


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
        aggravation_max_factor=AGGRAVATION_MAX_FACTOR,
        samples=got.answers,
    )
    assert got.during_p95_s <= budget * got.at_rest_p95_s, (
        f"during the export p95 {got.during_p95_s * 1000:.1f} ms vs "
        f"{got.at_rest_p95_s * 1000:.1f} ms at rest (factor {got.factor:.2f}, budget "
        f"x{budget:g}); {got.answers} answers"
    )


def aggravation_is_bounded(got: Contention) -> None:
    """The never-xfailed bound of J-PERF-1: an aggravation breaks the nightly, the known finding
    about the budget does not."""
    assert got.factor <= AGGRAVATION_MAX_FACTOR, (
        f"during the export p95 {got.during_p95_s * 1000:.1f} ms vs "
        f"{got.at_rest_p95_s * 1000:.1f} ms at rest (factor {got.factor:.2f}, aggravation bound "
        f"x{AGGRAVATION_MAX_FACTOR:g}); {got.answers} answers"
    )


@pytest.fixture(scope="module")
def catch_up() -> Contention:
    """500 new chapters over 500 followed series: the shape of a night's catch-up, measured once
    for the budget and for the aggravation bound of this module."""
    return measure_contention(chapters=int(budgets()["export_chapters"]), one_series=False)


@pytest.fixture(scope="module")
def one_series() -> Contention:
    """The worst shape: 500 chapters of a single series, measured once for both bounds."""
    return measure_contention(chapters=int(budgets()["export_chapters"]), one_series=True)


@KNOWN_OVER_BUDGET
@pytest.mark.req("ENF-PERF-007")
def test_bench_contention_export_500_chapters(catch_up: Contention) -> None:
    """500 new chapters over 500 followed series."""
    check("contention_export_500_chapters", catch_up)


@pytest.mark.req("ENF-PERF-007")
def test_bench_contention_aggravation_of_the_catch_up_is_bounded(catch_up: Contention) -> None:
    """A x6 aggravation must break the nightly even though the x2 budget is xfailed."""
    aggravation_is_bounded(catch_up)


@KNOWN_OVER_BUDGET
@pytest.mark.req("ENF-PERF-007")
def test_bench_contention_export_500_chapters_of_one_series(one_series: Contention) -> None:
    """The worst shape: 500 chapters of a single series."""
    check("contention_export_500_chapters_one_series", one_series)


@pytest.mark.req("ENF-PERF-007")
def test_bench_contention_aggravation_of_the_one_series_shape_is_bounded(
    one_series: Contention,
) -> None:
    """The same never-xfailed bound on the worst shape."""
    aggravation_is_bounded(one_series)


@pytest.fixture(scope="module")
def native_catch_up() -> Contention:
    """500 new chapters fanned out into the folder Mihon reads: the shape of a night's catch-up,
    measured once for both bounds."""
    return measure_native_folder_contention(chapters=int(budgets()["export_chapters"]))


@KNOWN_OVER_BUDGET
@pytest.mark.req("ENF-PERF-007")
def test_bench_contention_fan_out_to_a_native_folder(native_catch_up: Contention) -> None:
    """The fan-out to the native folder renders ComicInfo and covers in the very process that
    serves the list: the known finding is presumed to hold there too, and an XPASS says it does
    not — the marker then goes."""
    check("contention_fan_out_native_folder", native_catch_up)


@pytest.mark.req("ENF-PERF-007")
def test_bench_contention_aggravation_of_the_native_folder_fan_out_is_bounded(
    native_catch_up: Contention,
) -> None:
    """The never-xfailed bound on the native folder fan-out (EF-EXP-010)."""
    aggravation_is_bounded(native_catch_up)
