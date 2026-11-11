"""EF-INT-003, ENF-OPS-014: one scan of `schedule`, run-once, idempotent, window-aware."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.jobs.ledger import JobLedger, PendingJob
from shelfhold.jobs.scheduler import Scheduler, jitter_for
from tests.fakes_clock import ManualClock


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class Recorder:
    """A dispatcher that remembers what it was asked to run, in order."""

    def __init__(self) -> None:
        self.jobs: list[PendingJob] = []

    def dispatch(self, job: PendingJob) -> str:
        self.jobs.append(job)
        return f"task-{len(self.jobs)}"

    @property
    def kinds(self) -> list[str]:
        return [job.kind for job in self.jobs]


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "jobs.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def row(engine: Engine, kind: str) -> sa.Row[tuple[datetime | None, ...]]:
    with engine.connect() as conn:
        found = conn.execute(
            sa.select(platform.schedule.c.last_run_at, platform.schedule.c.next_due_at).where(
                platform.schedule.c.job_kind == kind
            )
        ).one()
    return found


def make(engine: Engine, clock: ManualClock, dispatcher: Recorder, seed: str = "i1") -> Scheduler:
    return Scheduler(engine, clock=clock, seed=seed, dispatcher=dispatcher)


@pytest.mark.req("EF-INT-003")
def test_skipped_cron_tick_caught_up_by_last_run_at_scan(engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 10, 30))
    out = Recorder()
    scheduler = make(engine, clock, out)
    scheduler.add("updates_check", "0 * * * *")  # next due 11:00Z
    clock.set(utc(2026, 11, 4, 10, 59, 40))
    scheduler.scan()
    assert out.kinds == []
    # Huey's wake-ups fall on either side of 11:00 and none looks at that minute
    clock.set(utc(2026, 11, 4, 11, 1, 20))
    scheduler.scan()
    assert out.kinds == ["updates_check"]
    last_run, next_due = row(engine, "updates_check")
    assert last_run == utc(2026, 11, 4, 11, 1, 20)
    assert next_due == utc(2026, 11, 4, 12)


@pytest.mark.req("EF-INT-003")
def test_minute_seen_twice_runs_once(engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 10, 0))
    out = Recorder()
    scheduler = make(engine, clock, out)
    scheduler.add("updates_check", "*/5 * * * *")
    clock.set(utc(2026, 11, 4, 10, 5, 2))
    scheduler.scan()
    scheduler.scan()  # the same minute, seen again
    make(engine, clock, out).scan()  # even by another scheduler object on the same database
    assert out.kinds == ["updates_check"]
    with engine.connect() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(platform.job_view)).scalar()
    assert count == 1


@pytest.mark.req("EF-INT-003")
def test_missed_periodic_runs_exactly_once_after_downtime(engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 8, 0))
    out = Recorder()
    scheduler = make(engine, clock, out)
    scheduler.add("hourly", "0 * * * *")
    scheduler.add("every_ten", "*/10 * * * *")
    scheduler.add("daily", "15 9 * * *")
    scheduler.add("later", "0 12 * * *")
    clock.advance(hours=2, minutes=7)  # two hours down: 13 ten-minute ticks and 2 hours missed
    restarted = make(engine, clock, out)
    restarted.startup_scan()
    assert sorted(out.kinds) == ["daily", "every_ten", "hourly"]  # 'later' is not due yet
    restarted.scan()
    assert len(out.kinds) == 3
    assert row(engine, "hourly")[1] == utc(2026, 11, 4, 11)


@pytest.mark.req("EF-INT-003")
def test_disabled_and_scoped_schedules(engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 10, 0))
    out = Recorder()
    scheduler = make(engine, clock, out)
    scheduler.add("check", "0 * * * *", scope_key="source-a")
    scheduler.add("check", "0 * * * *", scope_key="source-b")
    scheduler.add("off", "0 * * * *", enabled=False)
    clock.advance(hours=1, minutes=1)
    scheduler.scan()
    assert sorted(job.scope_key for job in out.jobs) == ["source-a", "source-b"]
    assert "off" not in out.kinds


@pytest.mark.req("EF-INT-003")
def test_overlapping_occurrence_is_coalesced(engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 10, 0))
    out = Recorder()
    scheduler = make(engine, clock, out)
    scheduler.add("slow", "*/5 * * * *")
    clock.advance(minutes=6)
    scheduler.scan()
    clock.advance(minutes=5)  # the first run has not finished
    scheduler.scan()
    assert out.kinds == ["slow"]  # one active job per periodic, the schedule still advanced
    assert row(engine, "slow")[1] == utc(2026, 11, 4, 10, 15)


@pytest.mark.req("EF-INT-003")
def test_crash_between_commit_and_dispatch_is_redispatched(engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 10, 0))

    class Exploding:
        def dispatch(self, job: PendingJob) -> str:
            raise OSError("queue file is locked")

    scheduler = Scheduler(engine, clock=clock, seed="i1", dispatcher=Exploding())
    scheduler.add("check", "*/5 * * * *")
    clock.advance(minutes=6)
    with pytest.raises(OSError, match="locked"):
        scheduler.scan()
    out = Recorder()
    make(engine, clock, out).scan()  # the job row survived; the next scan sends it
    assert out.kinds == ["check"]
    assert JobLedger(engine, clock).undispatched() == []


@pytest.mark.req("ENF-OPS-014")
def test_window_job_waits_for_the_window(engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 12, 0))
    out = Recorder()
    scheduler = make(engine, clock, out)
    scheduler.add("store_verify", "0 1 * * *", windowed=True)  # 01:00 Paris is outside
    _, due = row(engine, "store_verify")
    assert due == utc(2026, 11, 5, 2)  # moved to the start of the next window (03:00 CET)
    with engine.connect() as conn:
        window = conn.execute(sa.select(platform.schedule.c.window)).scalar_one()
    assert window == {"start_utc": "2026-11-05T02:00:00Z", "end_utc": "2026-11-05T06:00:00Z"}
    clock.set(utc(2026, 11, 5, 1, 59))
    scheduler.scan()
    assert out.kinds == []
    clock.set(utc(2026, 11, 5, 2, 0, 5))
    scheduler.scan()
    assert out.kinds == ["store_verify"]
    _, next_due = row(engine, "store_verify")
    assert next_due == utc(2026, 11, 6, 2)


@pytest.mark.req("ENF-OPS-014")
def test_window_job_whose_night_was_missed_waits_for_the_next(engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 20, 0))
    out = Recorder()
    scheduler = make(engine, clock, out)
    scheduler.add("store_verify", "0 3 * * *", windowed=True)  # due 2026-11-05T02:00Z
    clock.set(utc(2026, 11, 5, 9, 0))  # the host was down all night
    make(engine, clock, out).startup_scan()
    assert out.kinds == []  # nothing touches /store at 10:00 local
    assert row(engine, "store_verify")[1] == utc(2026, 11, 6, 2)


@pytest.mark.req("ENF-OPS-014")
def test_window_jobs_priority_order(engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 20, 0))
    out = Recorder()
    scheduler = make(engine, clock, out)
    for kind in ("thumbnail_gc", "integrity_rolling", "wal_checkpoint", "store_verify"):
        scheduler.add(kind, "0 3 * * *", windowed=True)
    clock.set(utc(2026, 11, 5, 3))
    scheduler.scan()
    assert out.kinds == ["wal_checkpoint", "store_verify", "integrity_rolling", "thumbnail_gc"]
    assert all(job.window_end == utc(2026, 11, 5, 6) for job in out.jobs)


@pytest.mark.req("EF-INT-003")
def test_jitter_is_seeded_per_instance_and_bounded(engine: Engine) -> None:
    base = utc(2026, 11, 4, 11)
    offsets = {jitter_for(f"inst-{n}", "check", "", base, 300) for n in range(20)}
    assert all(0 <= o <= 300 for o in offsets)
    assert len(offsets) > 5  # instances do not all fire together
    assert jitter_for("inst-1", "check", "", base, 300) == jitter_for(
        "inst-1", "check", "", base, 300
    )
    assert jitter_for("inst-1", "check", "", base, 0) == 0

    clock = ManualClock(utc(2026, 11, 4, 10, 30))
    scheduler = make(engine, clock, Recorder(), seed="inst-7")
    scheduler.add("check", "0 * * * *", jitter_s=300)
    _, due = row(engine, "check")
    assert 0 <= (due - base).total_seconds() <= 300
    assert (due - base).total_seconds() == jitter_for("inst-7", "check", "", base, 300)


@pytest.mark.req("ENF-LEG-007")
def test_default_schedule_offset_differs_between_instances(tmp_path: Path) -> None:
    """The default offset of a periodic comes from the instance seed: two installations that
    register the same cron do not hit a source on the same second."""
    clock = ManualClock(utc(2026, 1, 1, 0, 0, 0))
    due: list[datetime | None] = []
    for name, seed in (("a", "instance-aaaa"), ("b", "instance-bbbb"), ("a2", "instance-aaaa")):
        engine = create_db_engine(tmp_path / f"{name}.db")
        metadata.create_all(engine)
        make(engine, clock, Recorder(), seed).add_default("source.refresh", "0 * * * *")
        due.append(row(engine, "source.refresh").next_due_at)
        engine.dispose()
    assert due[0] != due[1]
    assert due[0] == due[2]  # a restart of the same instance agrees with itself
