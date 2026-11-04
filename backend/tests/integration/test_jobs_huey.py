"""EF-INT-003: one Huey instance per pool, one periodic, flush_locks after the instance lock."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.jobs.huey_app import InstanceLockNotHeldError, JobPools, boot_jobs
from tests.fakes_clock import ManualClock

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


class Lock:
    def __init__(self, held: bool) -> None:
        self.held = held

    def is_held(self) -> bool:
        return self.held


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "shelfhold.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def pools(tmp_path: Path, engine: Engine, clock: ManualClock) -> JobPools:
    return JobPools(tmp_path / "data", engine=engine, clock=clock, seed="instance-1")


def statuses(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(platform.job_view.c.kind, platform.job_view.c.status))
        return sorted((r.kind, r.status) for r in rows)


@pytest.mark.req("EF-INT-003")
def test_one_huey_instance_per_pool(pools: JobPools, tmp_path: Path) -> None:
    assert pools.names == ("io", "cpu", "maint")
    instances = [pools.huey(name) for name in pools.names]
    assert len({id(h) for h in instances}) == 3
    assert len({h.name for h in instances}) == 3
    files = {Path(h.storage.filename) for h in instances}  # type: ignore[attr-defined]
    assert len(files) == 3
    assert all(f.parent == tmp_path / "data" for f in files)
    with pytest.raises(KeyError):
        pools.huey("gpu")


@pytest.mark.req("EF-INT-003")
def test_a_single_periodic_task_scans_the_schedule(pools: JobPools, clock: ManualClock) -> None:
    assert pools.periodic_tasks() == [("io", "tick")]
    for minute in (0, 1, 31, 59):
        assert pools.tick_matches(datetime(2026, 11, 4, 12, minute, tzinfo=UTC))
    pools.scheduler.add("updates_check", "0 * * * *")
    pools.register("updates_check", pool="io", handler=lambda job: None)
    clock.advance(hours=1, minutes=1)
    pools.tick()
    assert pools.huey("io").pending_count() == 1
    pools.tick()  # the same schedule again: nothing more
    assert pools.huey("io").pending_count() == 1


@pytest.mark.req("EF-INT-003")
def test_job_runs_in_its_pool_and_is_tracked(pools: JobPools, engine: Engine) -> None:
    ran: list[str] = []
    pools.register("thumbs", pool="cpu", handler=lambda job: ran.append(job.dedupe_key))
    pools.register("boom", pool="io", handler=lambda job: 1 / 0)
    pools.submit("thumbs", dedupe_key="thumb:c1")
    pools.submit("boom", dedupe_key="boom:1")
    assert pools.huey("cpu").pending_count() == 1
    assert pools.huey("io").pending_count() == 1
    assert pools.huey("maint").pending_count() == 0
    for name in ("cpu", "io"):
        task = pools.huey(name).dequeue()
        assert task is not None
        pools.huey(name).execute(task)
    assert ran == ["thumb:c1"]
    assert statuses(engine) == [("boom", "failed"), ("thumbs", "succeeded")]


@pytest.mark.req("EF-INT-003")
def test_job_idempotent_per_chapter(pools: JobPools, engine: Engine) -> None:
    pools.register("download_chapter", pool="io", handler=lambda job: None)
    first = pools.submit("download_chapter", dedupe_key="chapter:src:c42")
    again = pools.submit("download_chapter", dedupe_key="chapter:src:c42")
    other = pools.submit("download_chapter", dedupe_key="chapter:src:c43")
    assert first.job_id == again.job_id
    assert first.job_id != other.job_id
    assert pools.huey("io").pending_count() == 2
    assert statuses(engine) == [("download_chapter", "queued")] * 2


@pytest.mark.req("EF-INT-003")
def test_busy_domain_slot_defers_the_job(pools: JobPools, engine: Engine, clock: ManualClock) -> None:
    ran: list[str] = []
    slots = pools.slots
    slots.declare("example.org", 1)
    pools.register("fetch", pool="io", handler=lambda job: ran.append(job.dedupe_key))
    assert slots.try_acquire("example.org")  # another job holds the only slot
    pools.submit("fetch", dedupe_key="f1", limit_key="example.org")
    huey = pools.huey("io")
    task = huey.dequeue()
    assert task is not None
    huey.execute(task)
    assert ran == []
    assert statuses(engine) == [("fetch", "queued")]
    assert huey.scheduled_count() == 1  # retried later, no worker thread kept waiting
    slots.release("example.org")


@pytest.mark.req("EF-INT-003", "ENF-OPS-007")
def test_flush_locks_at_boot(pools: JobPools, engine: Engine) -> None:
    for name in pools.names:
        pools.huey(name).lock_task("job:orphan").acquire()  # a kill -9 left these behind
    slots = pools.slots
    slots.declare("example.org", 1)
    slots.try_acquire("example.org")
    pools.register("fetch", pool="io", handler=lambda job: None)
    pools.submit("fetch", dedupe_key="chapter:1")
    pools.huey("io").lock_task("job:chapter:1").acquire()
    with engine.begin() as conn:  # the worker died while running another job
        conn.execute(
            sa.insert(platform.job_view).values(
                id="01JBZ3X5QH8M2N4P6R7S9T0V1W", kind="fetch", status="running", dedupe_key="k2"
            )
        )

    refused = Lock(held=False)
    with pytest.raises(InstanceLockNotHeldError):
        boot_jobs(pools, instance_lock=refused)
    assert all(pools.huey(n).is_locked("job:orphan") for n in pools.names)  # nothing touched
    assert slots.in_use("example.org") == 1

    report = boot_jobs(pools, instance_lock=Lock(held=True))
    assert not any(pools.huey(n).is_locked("job:orphan") for n in pools.names)
    assert not pools.huey("io").is_locked("job:chapter:1")
    assert slots.in_use("example.org") == 0
    assert report.requeued == 1  # the 'running' row goes back to the queue, idempotent per chapter
    assert ("fetch", "running") not in statuses(engine)


@pytest.mark.req("EF-INT-003")
def test_boot_catches_up_once(pools: JobPools, clock: ManualClock) -> None:
    pools.register("updates_check", pool="io", handler=lambda job: None)
    pools.scheduler.add("updates_check", "0 * * * *")
    clock.advance(hours=3)
    report = boot_jobs(pools, instance_lock=Lock(held=True))
    assert report.queued == 1
    assert pools.huey("io").pending_count() == 1
