"""ENF-PERF-008 (culpable): the nightly jobs fit in the four-hour window, or pick up again at their
cursor the next night (E9).

The reference gives, for the profile (60 000 Units), the time each window job may take: storage
check 30 min, hash comparison 1 h, rolling integrity 2 h, 3 h 30 in all against the 4 h of the
default window (ENF-OPS-014). A job is cut into steps of `UNITS_PER_STEP` Units, each costing its
share of that budget on an injected clock; they run through the real scheduler, the real Huey
pool and the real cursor (`JobContext.run_steps`, L24). Nothing sleeps and nothing touches a disk:
the bench proves the budgets add up and that a slow night loses no work, not the speed of a disk
(that one is measured before a release, on the full profile).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.jobs.huey_app import JobPools
from shelfhold.jobs.window import DEFAULT_WINDOW, WINDOW_JOB_ORDER
from tests.bench.generator import CI_PROFILE
from tests.bench.harness import load_reference
from tests.fakes_clock import ManualClock

UNITS_PER_STEP = 200
STEPS = CI_PROFILE.units // UNITS_PER_STEP  # 300 steps per job
JOBS = ("store_verify", "hash_compare", "integrity_rolling")
HOUR = 3600


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "shelfhold.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def run_all(pools: JobPools, pool: str) -> None:
    huey = pools.huey(pool)
    while (task := huey.dequeue()) is not None:
        huey.execute(task)


class Night:
    """The three window jobs of the reference; `slowness` says how many times slower than planned
    a job runs (a slow disk, a big library)."""

    def __init__(self, tmp_path: Path, engine: Engine, slowness: dict[str, float]) -> None:
        budgets = load_reference()["budgets"]["jobs_s"]
        self.clock = ManualClock(utc(2026, 11, 4, 12))
        (tmp_path / "data").mkdir(parents=True)
        self.pools = JobPools(tmp_path / "data", engine=engine, clock=self.clock, seed="bench")
        self.engine = engine
        self.log: list[tuple[str, int]] = []  # (job, step), in the order they ran
        for kind in JOBS:
            cost = budgets[kind] * slowness.get(kind, 1.0) / STEPS
            steps = [self._step(kind, n, cost) for n in range(STEPS)]
            self.pools.register(
                kind, pool="maint", handler=lambda ctx, s=steps: ctx.run_steps(s), windowed=True
            )
            self.pools.scheduler.add(kind, "0 3 * * *", windowed=True)

    def _step(self, kind: str, n: int, cost: float) -> Callable[[], None]:
        def run() -> None:
            self.log.append((kind, n))
            self.clock.advance(seconds=cost)  # a step is never cut in the middle

        return run

    def night(self, day: int) -> tuple[datetime, datetime]:
        """Open the window of 2026-11-`day` (03:00 Paris, 02:00 UTC), run what is due, and return
        (opened, last instant of work)."""
        opened = utc(2026, 11, day, 2, 0, 5)
        self.clock.set(opened)
        self.pools.tick()
        run_all(self.pools, "maint")
        return opened, self.clock.now()

    def rows(self) -> dict[str, sa.Row[tuple[object, ...]]]:
        with self.engine.connect() as connection:
            return {r.kind: r for r in connection.execute(sa.select(platform.job_view))}

    def steps_of(self, kind: str) -> list[int]:
        return [n for k, n in self.log if k == kind]


@pytest.mark.req("ENF-PERF-008")
def test_bench_window_budgets_add_up_to_the_default_window() -> None:
    budgets = load_reference()["budgets"]
    window = DEFAULT_WINDOW.bounds_on(datetime(2026, 11, 5).date())
    assert budgets["window_s"] == (window.end - window.start).total_seconds() == 4 * HOUR
    assert set(budgets["jobs_s"]) == set(JOBS) <= set(WINDOW_JOB_ORDER)
    assert budgets["jobs_s"]["store_verify"] <= 30 * 60  # storage check, full profile (§7.0)
    assert budgets["jobs_s"]["integrity_rolling"] <= 2 * HOUR  # rolling integrity per night
    assert sum(budgets["jobs_s"].values()) <= budgets["window_s"]


@pytest.mark.req("ENF-PERF-008")
@pytest.mark.parametrize(
    ("slowness", "nights"),
    [({}, 1), ({"integrity_rolling": 2.0}, 2)],
    ids=["planned-speed", "slow-integrity"],
)
def test_bench_window_jobs_fit_or_resume_next_night(
    tmp_path: Path, engine: Engine, slowness: dict[str, float], nights: int
) -> None:
    """At the planned speed one night is enough. When the rolling integrity takes twice as long
    (4 h for a 4 h window already used by the others), it stops with the window, and the next
    night picks it up at its cursor: no step is done twice, none is skipped."""
    budgets = load_reference()["budgets"]
    one_step_s = 2 * max(budgets["jobs_s"].values()) / STEPS
    night = Night(tmp_path, engine, slowness)
    for day in range(5, 5 + nights):
        opened, stopped = night.night(day)
        # a step is never cut: a night may end one step after the window closes, not more
        assert (stopped - opened).total_seconds() <= budgets["window_s"] + one_step_s
        parked = night.rows()["integrity_rolling"]
        if day < 4 + nights:
            assert (parked.status, parked.error_code) == ("paused", "window.preempted")
            assert parked.cursor is not None
        else:
            assert parked.status == "succeeded"
    # the daily jobs run again each night; the parked one continues instead of starting over
    assert night.steps_of("store_verify") == list(range(STEPS)) * nights
    assert night.steps_of("hash_compare") == list(range(STEPS)) * nights
    assert night.steps_of("integrity_rolling") == list(range(STEPS))
    started = [k for k, n in night.log if n == 0]
    assert started[:3] == sorted(JOBS, key=WINDOW_JOB_ORDER.index)  # priority order
