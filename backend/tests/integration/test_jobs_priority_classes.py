"""EF-INT-017 (M1b, passe 11): three fixed classes, new > request > backfill; the backlog is cut
into batches and an old one is not starved."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.jobs.huey_app import BACKFILL_MAX_WAIT, JobContext, JobPools
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
    return JobPools(tmp_path / "data", engine=engine, clock=clock, seed="i")


def drain(pools: JobPools, order: list[str]) -> None:
    huey = pools.huey("io")
    while (task := huey.dequeue()) is not None:
        huey.execute(task)


def register(pools: JobPools, order: list[str]) -> None:
    def handler(context: JobContext) -> None:
        order.append(context.dedupe_key)

    pools.register("fetch", pool="io", handler=handler)


@pytest.mark.req("EF-INT-017")
def test_huey_priority_classes_new_before_backfill(pools: JobPools) -> None:
    order: list[str] = []
    register(pools, order)
    for n in range(4):  # a catch-up of chapters is already in the queue...
        pools.submit("fetch", dedupe_key=f"backfill-{n}", priority_class="backfill")
    pools.submit("fetch", dedupe_key="asked", priority_class="request")
    pools.submit("fetch", dedupe_key="new-chapter", priority_class="new")  # ...and a new one comes
    drain(pools, order)
    assert order[:2] == ["new-chapter", "asked"]
    assert sorted(order[2:]) == [f"backfill-{n}" for n in range(4)]


@pytest.mark.req("EF-INT-017")
def test_backfill_waiting_too_long_is_promoted_not_starved(
    pools: JobPools, engine: Engine, clock: ManualClock
) -> None:
    order: list[str] = []
    register(pools, order)
    pools.submit("fetch", dedupe_key="old-backfill", priority_class="backfill")
    clock.advance(seconds=BACKFILL_MAX_WAIT.total_seconds() - 60)
    pools.submit("fetch", dedupe_key="young-backfill", priority_class="backfill")
    assert pools.promote_aged_backfill() == 0  # nobody has waited the bound yet
    clock.advance(seconds=120)
    assert pools.promote_aged_backfill() == 1  # only the old one
    pools.submit("fetch", dedupe_key="new-chapter", priority_class="new")
    drain(pools, order)
    assert order == ["new-chapter", "old-backfill", "young-backfill"]
    with engine.connect() as conn:
        classes = dict(
            conn.execute(
                sa.select(platform.job_view.c.dedupe_key, platform.job_view.c.priority_class)
            ).all()
        )
    assert classes["old-backfill"] == "request"  # served at the class above, from then on
    assert classes["young-backfill"] == "backfill"
    assert BACKFILL_MAX_WAIT <= timedelta(hours=24)


@pytest.mark.req("EF-INT-017")
def test_the_tick_promotes_the_aged_backfill(
    pools: JobPools, engine: Engine, clock: ManualClock
) -> None:
    order: list[str] = []
    register(pools, order)
    pools.submit("fetch", dedupe_key="old", priority_class="backfill")
    clock.advance(seconds=BACKFILL_MAX_WAIT.total_seconds() + 1)
    pools.tick()
    with engine.connect() as conn:
        (cls,) = conn.execute(sa.select(platform.job_view.c.priority_class)).scalars()
    assert cls == "request"
    drain(pools, order)
    assert order == ["old"]  # the first task was consumed, the duplicate is harmless
