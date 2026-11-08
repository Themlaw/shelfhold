"""ENF-OPS-016: `PRAGMA optimize` daily, WAL checkpoint `TRUNCATE` at night, `VACUUM` monthly in
the window, for the database and for each Huey queue; never while a backup or a long write holds
the database, never while the instance is frozen."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.jobs.huey_app import JobPools
from shelfhold.jobs.maintenance import (
    CHECKPOINT_KIND,
    OPTIMIZE_KIND,
    VACUUM_EVERY,
    VACUUM_KIND,
    MaintenanceGate,
    SqliteMaintenance,
    register_sqlite_maintenance,
)
from tests.fakes_clock import ManualClock


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class Space:
    """The disk guard's answer, set by the test."""

    def __init__(self, room: bool = True) -> None:
        self.room = room

    def has_room_for_vacuum(self, *, database_bytes: int, volume: str) -> bool:
        return self.room


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    (tmp_path / "data").mkdir()
    engine = create_db_engine(tmp_path / "data" / "shelfhold.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(utc(2026, 11, 4, 12))  # noon in Paris


@pytest.fixture
def pools(tmp_path: Path, engine: Engine, clock: ManualClock) -> JobPools:
    return JobPools(tmp_path / "data", engine=engine, clock=clock, seed="m")


def wal_size(path: Path) -> int:
    wal = Path(f"{path}-wal")
    return wal.stat().st_size if wal.exists() else 0


def fatten(engine: Engine, rows: int = 400) -> None:
    with engine.begin() as conn:
        for _ in range(rows):
            conn.execute(
                sa.insert(platform.event).values(
                    type="x", schema_version=1, payload={"pad": "p" * 2000},
                    occurred_at=utc(2026, 1, 1), visibility="ops",
                )
            )  # fmt: skip


def make(pools: JobPools, engine: Engine, tmp_path: Path, space: Space | None = None, **kw):
    return SqliteMaintenance(
        engine, queue_files=pools.queue_files(), clock=pools.clock, window=pools.window,
        space=space or Space(), gate=kw.pop("gate", MaintenanceGate()), **kw,
    )  # fmt: skip


@pytest.mark.req("ENF-OPS-016")
def test_nightly_wal_checkpoint_truncates(pools: JobPools, engine: Engine, tmp_path: Path) -> None:
    main = tmp_path / "data" / "shelfhold.db"
    fatten(engine)
    assert wal_size(main) > 100_000
    report = make(pools, engine, tmp_path).checkpoint()
    assert wal_size(main) == 0
    assert report.databases == 4  # the main database and the three queues
    assert report.partial == []


@pytest.mark.req("ENF-OPS-016")
def test_nightly_checkpoint_when_a_reader_blocks_the_truncation(
    pools: JobPools, engine: Engine, tmp_path: Path
) -> None:
    fatten(engine)
    reader = engine.connect()  # a transaction that stays open: a snapshot of the log
    reader.execute(sa.select(sa.func.count()).select_from(platform.event)).all()
    fatten(engine, 10)  # written after the reader's snapshot: the log cannot be reset yet
    report = make(pools, engine, tmp_path).checkpoint()
    reader.close()
    assert report.partial == ["main"]  # said, not raised; tomorrow's checkpoint finishes
    assert make(pools, engine, tmp_path).checkpoint().partial == []


@pytest.mark.req("ENF-OPS-016")
def test_optimize_runs_daily_and_touches_no_data(
    pools: JobPools, engine: Engine, tmp_path: Path
) -> None:
    fatten(engine, 20)
    before = _count(engine)
    assert make(pools, engine, tmp_path).optimize() >= 4
    assert _count(engine) == before


def _count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(sa.select(sa.func.count()).select_from(platform.event)).scalar_one()
        )


@pytest.mark.req("ENF-OPS-016")
def test_monthly_vacuum_only_in_window(
    pools: JobPools, engine: Engine, clock: ManualClock, tmp_path: Path
) -> None:
    fatten(engine)
    with engine.begin() as conn:
        conn.execute(sa.delete(platform.event))
    main = tmp_path / "data" / "shelfhold.db"
    maintenance = make(pools, engine, tmp_path)
    maintenance.checkpoint()  # the deleted pages are in the file, free
    size_before = main.stat().st_size
    outside = maintenance.vacuum()  # noon: refused
    assert outside.outcome == "outside_window"
    assert main.stat().st_size == size_before
    clock.set(utc(2026, 11, 5, 3, 30))  # 04:30 in Paris
    inside = maintenance.vacuum()
    assert inside.outcome == "done" and inside.databases >= 4
    assert main.stat().st_size < size_before


@pytest.mark.req("ENF-OPS-016")
def test_vacuum_waits_for_a_backup_a_long_write_and_the_disk(
    pools: JobPools, engine: Engine, clock: ManualClock, tmp_path: Path
) -> None:
    clock.set(utc(2026, 11, 5, 3, 30))
    gate, space = MaintenanceGate(), Space(room=False)
    maintenance = make(pools, engine, tmp_path, space, gate=gate)
    assert maintenance.vacuum().outcome == "no_room"  # VACUUM needs twice the size free
    space.room = True
    with gate.hold("backup"):  # the future backup (L23) holds the gate for its whole run
        assert maintenance.vacuum().outcome == "busy"
        assert gate.holders() == ["backup"]
    with gate.hold("long_write"):
        assert maintenance.vacuum().outcome == "busy"
    # a job still running is a long write too
    pools.register("export.run", pool="io", handler=lambda context: None)
    job = pools.submit("export.run", dedupe_key="export.run")
    assert pools.ledger.claim_running(job.job_id)
    assert maintenance.vacuum(own_job=None).outcome == "busy"
    assert maintenance.vacuum(own_job=job.job_id).outcome == "done"


@pytest.mark.req("ENF-OPS-016")
def test_maintenance_respects_the_freeze(
    tmp_path: Path, engine: Engine, clock: ManualClock
) -> None:
    clock.set(utc(2026, 11, 5, 3, 30))
    freeze = FreezeState(process=True)
    pools = JobPools(tmp_path / "data", engine=engine, clock=clock, seed="f", freeze=freeze)
    maintenance = make(pools, engine, tmp_path, freeze=freeze)
    assert maintenance.vacuum().outcome == "frozen"
    assert maintenance.checkpoint().frozen
    # and the scheduler holds the periodic itself
    register_sqlite_maintenance(pools, engine, space=Space(), gate=MaintenanceGate(), freeze=freeze)
    clock.set(utc(2026, 11, 6, 4))  # 05:00 in Paris: the daily jobs are due
    pools.tick()
    assert pools.huey("maint").pending_count() + pools.huey("io").pending_count() == 0
    pools.scheduler.release()
    pools.tick()
    assert pools.huey("maint").pending_count() + pools.huey("io").pending_count() == 3


@pytest.mark.req("ENF-OPS-016")
def test_maintenance_jobs_are_registered_in_the_right_places(
    pools: JobPools, engine: Engine, tmp_path: Path
) -> None:
    register_sqlite_maintenance(pools, engine, space=Space(), gate=MaintenanceGate())
    with engine.connect() as conn:
        rows = conn.execute(sa.select(platform.schedule)).all()
    windowed = {r.job_kind: r.window is not None for r in rows}
    assert windowed == {OPTIMIZE_KIND: False, CHECKPOINT_KIND: True, VACUUM_KIND: True}
    crons = {r.job_kind: r.cron for r in rows}
    assert timedelta(days=28) <= VACUUM_EVERY  # a try every night, a success every month
    assert crons[OPTIMIZE_KIND].endswith("* * *") and crons[CHECKPOINT_KIND].endswith("* * *")
    # the checkpoint and the VACUUM use the window's own order (ENF-OPS-014)
    from shelfhold.jobs.window import WINDOW_JOB_ORDER

    assert CHECKPOINT_KIND in WINDOW_JOB_ORDER and VACUUM_KIND in WINDOW_JOB_ORDER


@pytest.mark.req("ENF-OPS-016")
def test_the_stepwise_runner_vacuums_each_database_between_two_steps(
    pools: JobPools, engine: Engine, clock: ManualClock, tmp_path: Path
) -> None:
    """`run_steps` has its production caller here: the window closes between two databases."""
    register_sqlite_maintenance(pools, engine, space=Space(), gate=MaintenanceGate())
    clock.set(utc(2026, 11, 5, 3, 30))
    job = pools.submit(VACUUM_KIND, dedupe_key="manual-vacuum")
    huey = pools.huey("maint")
    while (task := huey.dequeue()) is not None:
        huey.execute(task)
    with engine.connect() as conn:
        status = conn.execute(
            sa.select(platform.job_view.c.status).where(platform.job_view.c.id == job.job_id)
        ).scalar_one()
    assert status == "succeeded"
