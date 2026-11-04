"""Review of L12 (PR #35): double execution, window jobs, upsert, last_run_at, 0600 files."""

import stat
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.jobs.huey_app import CLASS_PRIORITY, TICK_PRIORITY, JobContext, JobPools
from tests.fakes_clock import ManualClock


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "shelfhold.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(utc(2026, 11, 4, 12))


@pytest.fixture
def pools(tmp_path: Path, engine: Engine, clock: ManualClock) -> JobPools:
    (tmp_path / "data").mkdir()
    return JobPools(tmp_path / "data", engine=engine, clock=clock, seed="i1")


def run_all(pools: JobPools, pool: str) -> None:
    huey = pools.huey(pool)
    while (task := huey.dequeue()) is not None:
        huey.execute(task)


def schedule_row(engine: Engine, kind: str) -> sa.Row[tuple[object, ...]]:
    with engine.connect() as conn:
        return conn.execute(
            sa.select(platform.schedule).where(platform.schedule.c.job_kind == kind)
        ).one()


@pytest.mark.req("EF-INT-003")
def test_same_job_dispatched_twice_runs_once(pools: JobPools) -> None:
    ran: list[str] = []
    pools.register("fetch", pool="io", handler=lambda job: ran.append(job.job_id))
    created = pools.submit("fetch", dedupe_key="chapter:1")
    # a crash between enqueue and mark_dispatched: the row is sent a second time
    pools.dispatch(pools.ledger.get(created.job_id))
    assert pools.huey("io").pending_count() == 2
    run_all(pools, "io")
    assert ran == [created.job_id]


@pytest.mark.req("EF-INT-003")
def test_finished_job_is_not_run_by_a_late_duplicate(pools: JobPools) -> None:
    ran: list[str] = []
    pools.register("fetch", pool="io", handler=lambda job: ran.append(job.job_id))
    created = pools.submit("fetch", dedupe_key="chapter:1")
    run_all(pools, "io")
    pools.dispatch(pools.ledger.get(created.job_id))
    run_all(pools, "io")
    assert ran == [created.job_id]


@pytest.mark.req("ENF-OPS-014")
def test_window_job_is_not_run_outside_its_window(
    pools: JobPools, engine: Engine, clock: ManualClock
) -> None:
    seen: list[JobContext] = []
    pools.register("store_verify", pool="maint", handler=seen.append, windowed=True)
    pools.scheduler.add("store_verify", "0 3 * * *", windowed=True)  # due 2026-11-05T02:00Z
    clock.set(utc(2026, 11, 5, 2, 0, 5))
    pools.tick()
    assert pools.huey("maint").pending_count() == 1
    clock.set(utc(2026, 11, 5, 6, 30))  # the worker only got to it after the window closed
    run_all(pools, "maint")
    assert seen == []
    assert pools.huey("maint").scheduled_count() == 1  # replanned, not dropped
    with engine.connect() as conn:
        row = conn.execute(sa.select(platform.job_view)).one()
    assert row.status == "queued"
    assert row.created_at == utc(2026, 11, 5, 2, 0, 5)  # untouched
    assert row.window_end == utc(2026, 11, 6, 6)  # the next window, persisted
    clock.set(utc(2026, 11, 6, 2, 0, 10))
    pools._execute(row.id)
    assert [(c.kind, c.window_end) for c in seen] == [("store_verify", utc(2026, 11, 6, 6))]


@pytest.mark.req("ENF-OPS-014")
def test_window_is_never_deduced_from_the_priority_class(
    pools: JobPools, clock: ManualClock
) -> None:
    seen: list[JobContext] = []
    clock.set(utc(2026, 11, 5, 3))  # inside the window
    pools.register("catch_up", pool="io", handler=seen.append)
    pools.submit("catch_up", dedupe_key="b1", priority_class="backfill")
    run_all(pools, "io")
    assert [c.window_end for c in seen] == [None]


@pytest.mark.req("ENF-OPS-014")
def test_missed_window_on_a_weekly_cron_replays_next_night(
    pools: JobPools, engine: Engine, clock: ManualClock
) -> None:
    clock.set(utc(2026, 10, 30, 20))
    pools.register("store_verify", pool="maint", handler=lambda job: None, windowed=True)
    pools.scheduler.add("store_verify", "0 3 * * 1", windowed=True)  # Mondays
    clock.set(utc(2026, 11, 2, 9))  # Monday's night is over, nothing ran
    pools.tick()
    assert schedule_row(engine, "store_verify").next_due_at == utc(2026, 11, 3, 2)
    with engine.connect() as conn:
        events = conn.execute(sa.select(platform.event)).all()
    assert [(e.type, e.visibility) for e in events] == [("ops.window_missed", "ops")]
    assert "job_kind" in events[0].payload
    clock.set(utc(2026, 11, 3, 2, 0, 5))
    pools.tick()
    assert pools.huey("maint").pending_count() == 1
    assert schedule_row(engine, "store_verify").next_due_at == utc(2026, 11, 9, 2)


@pytest.mark.req("EF-INT-003")
def test_add_unchanged_keeps_due_and_last_run(
    pools: JobPools, engine: Engine, clock: ManualClock
) -> None:
    pools.register("check", pool="io", handler=lambda job: None)
    pools.scheduler.add("check", "0 * * * *")
    clock.set(utc(2026, 11, 4, 13, 0, 30))
    pools.tick()
    before = schedule_row(engine, "check")
    assert before.last_run_at == utc(2026, 11, 4, 13, 0, 30)
    clock.set(utc(2026, 11, 4, 13, 40))
    pools.scheduler.add("check", "0 * * * *")  # a restart registers everything again
    after = schedule_row(engine, "check")
    assert (after.id, after.next_due_at, after.last_run_at) == (
        before.id,
        before.next_due_at,
        before.last_run_at,
    )
    pools.scheduler.add("check", "30 * * * *")  # a changed cron is planned afresh
    changed = schedule_row(engine, "check")
    assert changed.id == before.id
    assert changed.next_due_at == utc(2026, 11, 4, 14, 30)
    assert changed.last_run_at == before.last_run_at


@pytest.mark.req("EF-INT-003")
def test_coalesced_occurrence_does_not_touch_last_run_at(
    pools: JobPools, engine: Engine, clock: ManualClock
) -> None:
    pools.register("slow", pool="io", handler=lambda job: None)
    pools.scheduler.add("slow", "*/5 * * * *")
    clock.set(utc(2026, 11, 4, 12, 6))
    pools.tick()
    clock.set(utc(2026, 11, 4, 12, 11))  # the first run is still queued
    pools.tick()
    row = schedule_row(engine, "slow")
    assert row.last_run_at == utc(2026, 11, 4, 12, 6)
    assert row.next_due_at == utc(2026, 11, 4, 12, 15)


@pytest.mark.req("EF-INT-003")
def test_tick_outranks_every_job_class(pools: JobPools) -> None:
    (periodic,) = pools.huey("io")._registry.periodic_tasks
    assert periodic.priority == TICK_PRIORITY
    assert max(CLASS_PRIORITY.values()) < TICK_PRIORITY


@pytest.mark.req("ENF-SEC-012")
def test_queue_files_are_private(pools: JobPools, tmp_path: Path) -> None:
    pools.register("fetch", pool="io", handler=lambda job: None)
    pools.submit("fetch", dedupe_key="c")
    files = sorted((tmp_path / "data").glob("huey-*.db*"))
    assert {"huey-cpu.db", "huey-io.db", "huey-maint.db"} <= {f.name for f in files}
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)


@pytest.mark.req("ENF-OPS-014")
def test_window_job_resumes_from_its_cursor_without_redoing_steps(
    pools: JobPools, engine: Engine, clock: ManualClock
) -> None:
    done: list[int] = []

    def step(n: int) -> Callable[[], None]:
        def run() -> None:
            done.append(n)
            clock.advance(seconds=50)  # an operation is never cut in the middle

        return run

    steps = [step(n) for n in range(5)]
    pools.register(
        "integrity_rolling", pool="maint", handler=lambda ctx: ctx.run_steps(steps), windowed=True
    )
    pools.scheduler.add("integrity_rolling", "0 3 * * *", windowed=True)
    clock.set(utc(2026, 11, 5, 5, 58))  # two minutes before the window closes
    pools.tick()
    run_all(pools, "maint")
    assert done == [0, 1, 2]  # the third began at 05:59:40 and ended after the end
    with engine.connect() as conn:
        row = conn.execute(sa.select(platform.job_view)).one()
    assert (row.status, row.error_code, row.cursor) == (
        "paused",
        "window.preempted",
        '{"next_index": 3}',
    )

    clock.set(utc(2026, 11, 6, 2, 0, 5))  # tomorrow's window
    pools.tick()  # the occurrence that is due continues the parked job, it is not a second one
    run_all(pools, "maint")
    assert done == [0, 1, 2, 3, 4]  # nothing done twice
    with engine.connect() as conn:
        rows = conn.execute(sa.select(platform.job_view)).all()
    assert [(r.status, r.cursor) for r in rows] == [("succeeded", None)]
