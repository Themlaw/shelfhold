"""Shared pieces of the M3 L13 tests: a database with its instance row, the jobs pools, and one
principal of every kind that may or may not touch the maintenance mode and the queue."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.jobs.huey_app import JobPools
from shelfhold.jobs.window import DEFAULT_WINDOW, MaintenanceWindow
from shelfhold.ops.maintenance import MaintenanceOps
from tests.fakes_clock import ManualClock

NOON = datetime(2026, 11, 12, 12, tzinfo=UTC)  # 13:00 in Paris: outside the default window


@dataclass
class Env:
    engine: Engine
    clock: ManualClock
    pools: JobPools
    ops: MaintenanceOps
    data_dir: Path

    def rows(
        self, table: sa.Table, *where: sa.ColumnElement[bool]
    ) -> list[sa.Row[tuple[object, ...]]]:
        with self.engine.connect() as conn:
            return list(conn.execute(sa.select(table).where(*where)))

    def job(self, job_id: str) -> sa.Row[tuple[object, ...]]:
        (row,) = self.rows(platform.job_view, platform.job_view.c.id == job_id)
        return row

    def run(self, pool: str = "io") -> None:
        """Every task now in the pool's queue reaches a worker (the way a consumer would)."""
        huey = self.pools.huey(pool)
        while (task := huey.dequeue()) is not None:
            huey.execute(task)

    def run_all(self) -> None:
        for pool in self.pools.names:
            self.run(pool)

    def principal(self, kind: str = "admin") -> Principal:
        return principal(kind, self.clock.now())

    @staticmethod
    def clock_hour() -> timedelta:
        return timedelta(hours=1)


def principal(kind: str, now: datetime) -> Principal:
    """`admin` (fresh session), `stale` (an admin session past the freshness window), `member`,
    `child`, `key` (an admin-scope API key), `device`, `cli` (the host)."""
    match kind:
        case "admin":
            return Principal(
                "-", "root", "admin", Credential.SESSION, session_id=new_ulid(), auth_time=now
            )
        case "stale":
            return Principal(
                "-", "root", "admin", Credential.SESSION, session_id=new_ulid(),
                auth_time=now - timedelta(hours=3),
            )  # fmt: skip
        case "member":
            return Principal(
                "-", "mia", "member", Credential.SESSION, session_id=new_ulid(), auth_time=now
            )
        case "child":
            return Principal(
                "-", "leo", "child", Credential.SESSION, session_id=new_ulid(), auth_time=now
            )
        case "key":
            return Principal(
                "-", "ci", "admin", Credential.API_KEY, key_id=new_ulid(), scopes=("admin",)
            )  # fmt: skip
        case "device":
            return Principal("-", "tv", "member", Credential.DEVICE_TOKEN, key_id=new_ulid())
        case "cli":
            return Principal("1000", "uid:1000", "admin", Credential.LOCAL_CLI)
    raise ValueError(kind)


@contextmanager
def env(
    tmp_path: Path,
    *,
    start: datetime = NOON,
    window: MaintenanceWindow = DEFAULT_WINDOW,
) -> Iterator[Env]:
    engine = create_db_engine(tmp_path / "shelfhold.db")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.insert(platform.instance).values(instance_id=new_ulid(), jurisdiction="FR"))
    clock = ManualClock(start)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    pools = JobPools(data, engine=engine, clock=clock, seed="l13", window=window)
    ops = MaintenanceOps(engine, clock, window=window, control=pools.control)
    try:
        yield Env(engine, clock, pools, ops, data)
    finally:
        engine.dispose()
