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
from shelfhold.infrastructure.schema import export, metadata, platform
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
    pools.register(
        "night", pool="maint", handler=lambda c: seen.append(c.clock.now()), windowed=True
    )
    pools.scheduler.add("night", "0 12 * * *", windowed=True)  # midday is outside: clamped
    with engine.connect() as conn:
        row = conn.execute(sa.select(platform.schedule)).one()
    assert row.next_due_at == utc(2026, 11, 5, 22)  # opens at 23:00 Paris
    clock.set(utc(2026, 11, 5, 22, 1))
    pools.tick()
    run_all(pools, "maint")
    assert seen == [utc(2026, 11, 5, 22, 1)]
    clock.set(utc(2026, 11, 5, 23, 30))  # past midnight in Paris, still the same occurrence
    assert window.span_containing(clock.now()) is not None


@pytest.mark.req("ENF-OPS-014")
def test_a_changed_window_replans_the_window_jobs(tmp_path: Path, engine: Engine) -> None:
    clock = ManualClock(utc(2026, 11, 4, 12))
    first = make_pools(
        tmp_path, engine, clock, MaintenanceWindow.from_text("03:00", "07:00", "UTC")
    )
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
        windowed = windowed_kinds(inst.engine)
        # the jobs that read the disks are all window jobs
        assert {KEEP_CATCHUP_KIND, STORE_INDEX_KIND, rj.RECONCILE_KIND, rj.RECONCILE_HASH_KIND} <= (
            windowed
        )
        ran = _ran_kinds(inst.engine)
        assert windowed.isdisjoint(ran)
        assert not [a for a in spy.accesses if "/store" in a or "/library" in a or "manga" in a]


def windowed_kinds(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(platform.schedule.c.job_kind, platform.schedule.c.window))
        return {r.job_kind for r in rows if r.window is not None}


def _ran_kinds(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(platform.job_view.c.kind).where(platform.job_view.c.status != "queued")
        )
        return {r.kind for r in rows}


@pytest.mark.req("ENF-OPS-014")
def test_event_driven_export_allowed_outside_window(settings: Settings, tmp_path: Path) -> None:
    """Non-regression: `export.run` (a new chapter, an approved request) is not a window job,
    and the guard of the window lets an event or a user through at any hour."""
    from shelfhold.jobs.window import StoreOrigin, ensure_store_access

    with instance(settings, tmp_path) as inst:
        clock = ManualClock(utc(2026, 11, 4, 11))
        pools = JobPools(settings.data_dir, engine=inst.engine, clock=clock, seed="e")
        rj.register_runtime_jobs(pools, inst.comp)
        assert rj.EXPORT_JOB_KIND not in windowed_kinds(inst.engine)
        ensure_store_access(StoreOrigin.EVENT, now=clock.now(), window=pools.window)
        ensure_store_access(StoreOrigin.USER_REQUEST, now=clock.now(), window=pools.window)


@pytest.mark.req("ENF-OPS-014", "EF-INT-023", "EF-EXP-043")
def test_foreign_writer_is_detected_outside_the_window(settings: Settings, tmp_path: Path) -> None:
    """The detection of another writer is a safety job: it never waits for the night. At noon,
    the export job (not a window job) pauses the series, and `status` would list it."""
    from tests.l50_env import seeded

    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        env.export()
        folder = env.exported_files()[0].parent
        (folder / "foreign-file.txt").write_text("not ours")
        env.add_chapter(series, "3")  # something to write: the detector looks at the tree first
        clock = ManualClock(utc(2026, 11, 4, 11))  # noon in Paris, far from 03:00-07:00
        pools = JobPools(settings.data_dir, engine=env.inst.engine, clock=clock, seed="d")
        rj.register_runtime_jobs(pools, env.inst.comp)
        assert rj.EXPORT_JOB_KIND not in windowed_kinds(env.inst.engine)
        pools.submit(rj.EXPORT_JOB_KIND, dedupe_key="detect")
        run_all(pools, "io")
        paused = [
            r.paused_reason for r in env.rows(export.export_series) if r.paused_reason is not None
        ]
        assert paused == ["foreign_writer"]


@pytest.mark.req("ENF-OPS-014")
def test_only_heavy_disk_work_is_a_window_job(settings: Settings, tmp_path: Path) -> None:
    """The explicit list of what waits for the window (ENF-OPS-014: the heavy work on /store and
    /library, and the upkeep of the databases). A safety or detection job (the probe of a root,
    the lease, the foreign writer, the exposure audit, the health, the removal of the parental
    barrier) is not here, and a new window job must be added here on purpose."""
    from shelfhold.jobs.maintenance import MaintenanceGate, register_sqlite_maintenance

    class Room:
        def has_room_for_vacuum(self, *, database_bytes: int, volume: str) -> bool:
            return True

    with instance(settings, tmp_path) as inst:
        pools = JobPools(
            settings.data_dir, engine=inst.engine, clock=ManualClock(utc(2026, 11, 4, 11)), seed="w"
        )
        rj.register_runtime_jobs(pools, inst.comp)
        register_sqlite_maintenance(pools, inst.engine, space=Room(), gate=MaintenanceGate())
        assert windowed_kinds(inst.engine) == {
            "reconcile.cycle",  # verification of the tree
            "reconcile.hash",  # hashing of files
            "store.keep_catchup",  # catch-up copies into /store
            "store.index",  # INDEX.jsonl of /store
            "library.safeguard",  # "Mettre à l'abri" and the optional upstream purge (L16)
            "retention.recycle",  # the bins of /library
            "integrity_rolling",  # the rolling integrity check of the archives (EF-BIB-012)
            "wal_checkpoint",
            "vacuum_monthly",
        }
