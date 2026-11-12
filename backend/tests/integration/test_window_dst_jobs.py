"""M3 L13, EF-INT-004: a heavy job never starts outside its window, on the days the clocks change in
either direction and for a window across midnight; and a job the window stopped keeps its
`window.preempted` discriminant through everything an operator can do to the queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shelfhold.infrastructure.schema import platform
from shelfhold.jobs.huey_app import JobContext
from shelfhold.jobs.ledger import PREEMPTED
from shelfhold.jobs.window import MaintenanceWindow
from tests.maintenance_env import Env, env

pytestmark = pytest.mark.req("EF-INT-004")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


STARTS = {"spring": utc(2026, 3, 27, 12), "autumn": utc(2026, 10, 23, 12)}


@pytest.mark.parametrize("bounds", [("03:00", "07:00"), ("01:00", "06:00"), ("23:00", "02:00")])
@pytest.mark.parametrize("season", ["spring", "autumn"])
def test_a_heavy_job_never_starts_outside_its_window_across_the_change_of_hour(
    tmp_path: Path, bounds: tuple[str, str], season: str
) -> None:
    window = MaintenanceWindow.from_text(*bounds, "Europe/Paris")
    start = STARTS[season]
    with env(tmp_path, start=start, window=window) as e:
        started: list[datetime] = []
        e.pools.register(
            "heavy", pool="maint", handler=lambda c: started.append(c.clock.now()), windowed=True
        )
        e.pools.scheduler.add("heavy", "*/20 * * * *", windowed=True)
        manual = 0
        for step in range(0, 4 * 24 * 6):  # four days, every ten minutes
            e.clock.set(start + timedelta(minutes=10 * step))
            e.pools.tick()
            if step % 7 == 0:  # an operator also queues one by hand, any hour
                manual += 1
                e.pools.submit("heavy", dedupe_key=f"manual-{manual}")
            e.run("maint")
        assert started, "the job never ran at all: the window was never found"
        for instant in started:
            assert window.span_containing(instant) is not None, instant
        nights = {window.span_containing(i) for i in started}
        assert len(nights) >= 3  # it ran in each night of the four days, on the changing one too


def test_preempted_job_keeps_window_discriminant(tmp_path: Path) -> None:
    window = MaintenanceWindow.from_text("03:00", "07:00", "Europe/Paris")
    with env(tmp_path, start=utc(2026, 11, 12, 2, 30), window=window) as e:  # 03:30 in Paris
        ran: list[int] = []

        def step(n: int, advance_h: int):  # type: ignore[no-untyped-def]
            def run() -> None:
                ran.append(n)
                e.clock.advance(hours=advance_h)

            return run

        def handler(context: JobContext) -> None:
            context.run_steps([step(0, 2), step(1, 2), step(2, 0), step(3, 0)])

        e.pools.register("night", pool="maint", handler=handler, windowed=True)
        e.pools.scheduler.add("night", "0 3 * * *", windowed=True)
        job = e.pools.submit("night", dedupe_key="periodic:night:").job_id
        e.run("maint")
        assert ran == [0, 1]  # the window closed at 07:00 Paris, between step 1 and step 2
        assert_parked(e, job, 2)
        # everything an operator can do to the queue leaves the discriminant and the cursor alone
        e.ops.pause(None, principal=e.principal("cli"))
        e.ops.resume(None, principal=e.principal("cli"))
        e.ops.enter(principal=e.principal("cli"))
        e.pools.tick()
        e.ops.leave(principal=e.principal("cli"))
        e.pools.tick()
        e.run("maint")
        assert ran == [0, 1]
        assert_parked(e, job, 2)
        # the next night resumes it where it stopped, as one job
        e.clock.set(utc(2026, 11, 13, 2, 10))
        e.pools.tick()
        e.run("maint")
        assert ran == [0, 1, 2, 3]
        assert e.job(job).status == "succeeded"
        assert len(e.rows(platform.job_view)) == 1  # one job, not one per night


def assert_parked(e: Env, job: str, next_index: int) -> None:
    row = e.job(job)
    assert (row.status, row.error_code) == ("paused", PREEMPTED)
    assert row.cursor == f'{{"next_index": {next_index}}}'
