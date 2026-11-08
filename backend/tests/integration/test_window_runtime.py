"""ENF-OPS-014 (M1b): a window set by the owner drives the scheduler; a periodic job reaches
neither /store nor /library outside it, an event-driven one may."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.jobs.huey_app import JobPools
from shelfhold.jobs.window import MaintenanceWindow
from shelfhold.store_jobs import KEEP_CATCHUP_KIND, STORE_INDEX_KIND, register_store_jobs
from tests.fakes_clock import ManualClock
from tests.fs_spy import FsSpy
from tests.l41_env import instance


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


def make_pools(tmp_path: Path, engine: Engine, clock: ManualClock, window: MaintenanceWindow):
    (tmp_path / "data").mkdir(exist_ok=True)
    return JobPools(tmp_path / "data", engine=engine, clock=clock, seed="i", window=window)


@pytest.mark.req("ENF-OPS-014")
def test_window_job_runs_in_a_window_across_midnight(tmp_path: Path, engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 12))
    window = MaintenanceWindow.from_text("23:00", "02:00", "Europe/Paris")
    pools = make_pools(tmp_path, engine, clock, window)
    seen: list[datetime] = []
    pools.register("night", pool="maint", handler=lambda c: seen.append(c.clock.now()), windowed=True)
    pools.scheduler.add("night", "30 3 * * *", windowed=True)  # 03:30 is outside: clamped
    with engine.connect() as conn:
        row = conn.execute(sa.select(platform.schedule)).one()
    assert row.next_due_at == utc(2026, 11, 4, 22)  # opens at 23:00 Paris
    clock.set(utc(2026, 11, 4, 22, 1))
    pools.tick()
    run_all(pools, "maint")
    assert seen == [utc(2026, 11, 4, 22, 1)]
    clock.set(utc(2026, 11, 5, 0, 30))  # past midnight, still the same occurrence
    assert window.span_containing(clock.now()) is not None


@pytest.mark.req("ENF-OPS-014")
def test_a_changed_window_replans_the_window_jobs(tmp_path: Path, engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 12))
    first = make_pools(tmp_path, engine, clock, MaintenanceWindow.from_text("03:00", "07:00", "UTC"))
    first.scheduler.add("night", "0 3 * * *", windowed=True)
    # the owner moves the window and restarts: the stored occurrence is the old one
    moved = JobPools(
        tmp_path / "data", engine=engine, clock=clock, seed="i",
        window=MaintenanceWindow.from_text("01:00", "04:00", "UTC"),
    )  # fmt: skip
    moved.scheduler.add("night", "0 3 * * *", windowed=True)
    with engine.connect() as conn:
        row = conn.execute(sa.select(platform.schedule)).one()
    assert row.window["start_utc"] == "2026-11-05T01:00:00Z"
    assert row.next_due_at == utc(2026, 11, 5, 3)


@pytest.mark.req("ENF-OPS-014")
def test_no_store_or_library_access_outside_window_unless_user_request(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        clock = ManualClock(utc(2026, 11, 4, 11))  # noon in Paris
        window = MaintenanceWindow.from_text("03:00", "07:00", "Europe/Paris")
        pools = JobPools(
            settings.data_dir, engine=inst.engine, clock=clock, seed="w", window=window
        )
        register_store_jobs(pools, inst.comp)
        rj.register_runtime_jobs(pools, inst.comp)
        watched = [inst.library.parent, settings.store_dir]
        with FsSpy(watched).watching() as spy:
            for minute in range(0, 60, 10):  # an hour of ticks at midday, with a task due or not
                clock.set(utc(2026, 11, 4, 11, minute))
                pools.tick()
                run_all(pools, "maint")
        windowed = {
            kind for kind in (KEEP_CATCHUP_KIND, STORE_INDEX_KIND, rj.RECONCILE_KIND,
                              rj.RECONCILE_HASH_KIND) if pools.is_windowed(kind)
        }  # fmt: skip
        assert len(windowed) == 4  # the jobs that read the disks are all window jobs
        ran = _ran_kinds(inst.engine)
        assert windowed.isdisjoint(ran)
        assert not [a for a in spy.accesses if "/store" in a or "/library" in a or "manga" in a]


def _ran_kinds(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(platform.job_view.c.kind).where(platform.job_view.c.status != "queued")
        )
        return {r.kind for r in rows}


@pytest.mark.req("ENF-OPS-014")
def test_event_driven_export_allowed_outside_window(tmp_path: Path, engine: Engine) -> None:
    """Non-regression: `export.run` (a new chapter, an approved request) is not a window job,
    and the guard of the window lets an event or a user through at any hour."""
    from shelfhold.jobs.window import StoreOrigin, ensure_store_access

    clock = ManualClock(utc(2026, 11, 4, 11))
    pools = make_pools(tmp_path, engine, clock, MaintenanceWindow.from_text("03:00", "07:00", "UTC"))
    pools.register(rj.EXPORT_JOB_KIND, pool="io", handler=lambda c: None)
    assert not pools.is_windowed(rj.EXPORT_JOB_KIND)
    ensure_store_access(StoreOrigin.EVENT, now=clock.now(), window=pools.window)
    ensure_store_access(StoreOrigin.USER_REQUEST, now=clock.now(), window=pools.window)
