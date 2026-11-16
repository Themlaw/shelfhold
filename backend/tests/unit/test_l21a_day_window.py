"""M4 L21a (ENF-LEG-005): the day of Google's quota ends at midnight in the Pacific, not in UTC.

Computed with `zoneinfo`, so that daylight saving time is the library's business: the day of the
spring change holds 23 hours and the day of the autumn change 25, and both are tried here. The
store that counts the budget and the pause it keeps for a spent day use the same window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher.day_window import (
    PACIFIC,
    day_key,
    next_midnight,
    pacific_midnight_after,
)
from shelfhold.infrastructure.fetcher.errors import ProviderQuotaExceeded
from shelfhold.infrastructure.fetcher.limiter import TokenBucket
from shelfhold.infrastructure.fetcher.quota_store import SqlBucketStore
from shelfhold.infrastructure.schema import metadata

pytestmark = pytest.mark.req("ENF-LEG-005")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_pacific_midnight_in_winter_is_08h_utc() -> None:
    assert pacific_midnight_after(utc(2026, 12, 1, 12)) == utc(2026, 12, 2, 8)
    assert pacific_midnight_after(utc(2026, 12, 2, 7, 59, 59)) == utc(2026, 12, 2, 8)


def test_pacific_midnight_in_summer_is_07h_utc() -> None:
    assert pacific_midnight_after(utc(2026, 7, 4, 12)) == utc(2026, 7, 5, 7)


def test_the_day_of_the_spring_change_holds_23_hours() -> None:
    # 2026-03-08: 02:00 PST becomes 03:00 PDT
    start = pacific_midnight_after(utc(2026, 3, 7, 20))
    assert start == utc(2026, 3, 8, 8)  # still PST at midnight
    end = pacific_midnight_after(start)
    assert end == utc(2026, 3, 9, 7)  # PDT by then
    assert end - start == timedelta(hours=23)


def test_the_day_of_the_autumn_change_holds_25_hours() -> None:
    # 2026-11-01: 02:00 PDT becomes 01:00 PST
    start = pacific_midnight_after(utc(2026, 10, 31, 12))
    assert start == utc(2026, 11, 1, 7)  # still PDT at midnight
    end = pacific_midnight_after(start)
    assert end == utc(2026, 11, 2, 8)  # PST by then
    assert end - start == timedelta(hours=25)


def test_the_instant_of_midnight_belongs_to_the_next_day() -> None:
    assert pacific_midnight_after(utc(2026, 11, 2, 8)) == utc(2026, 11, 3, 8)


def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(ValueError):
        pacific_midnight_after(datetime(2026, 11, 1, 12))


@pytest.mark.parametrize(
    ("before", "after", "last_day", "first_day"),
    [
        (utc(2026, 3, 8, 7, 59, 59), utc(2026, 3, 8, 8), "2026-03-07", "2026-03-08"),
        (utc(2026, 11, 1, 6, 59, 59), utc(2026, 11, 1, 7), "2026-10-31", "2026-11-01"),
        (utc(2026, 11, 2, 7, 59, 59), utc(2026, 11, 2, 8), "2026-11-01", "2026-11-02"),
        (utc(2026, 12, 1, 7, 59, 59), utc(2026, 12, 1, 8), "2026-11-30", "2026-12-01"),
    ],
)
def test_the_day_key_turns_at_pacific_midnight_only(
    before: datetime, after: datetime, last_day: str, first_day: str
) -> None:
    assert day_key(before.timestamp(), PACIFIC) == last_day
    assert day_key(after.timestamp(), PACIFIC) == first_day
    assert day_key(before.timestamp(), None) == before.date().isoformat()  # UTC when none is given


def test_next_midnight_of_a_zone_is_a_timestamp() -> None:
    assert next_midnight(utc(2026, 11, 1, 12).timestamp(), PACIFIC) == utc(2026, 11, 2, 8).timestamp()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = create_db_engine(tmp_path / "quota.db")
    metadata.create_all(engine)
    return engine


@pytest.mark.parametrize(
    "midnight", [utc(2026, 3, 8, 8), utc(2026, 11, 1, 7), utc(2026, 11, 2, 8), utc(2026, 12, 1, 8)]
)
def test_the_shared_budget_is_spent_per_pacific_day(engine: Engine, midnight: datetime) -> None:
    store = SqlBucketStore(engine)
    last = (midnight - timedelta(minutes=5)).timestamp()
    first = midnight.timestamp()

    def take(at: float) -> float:
        return store.reserve("googlebooks", 1000.0, 1000, 2, at, day_timezone=PACIFIC)

    take(last), take(last + 1)
    with pytest.raises(ProviderQuotaExceeded):  # the third of the day
        take(last + 2)
    take(first)  # one second into the new day: the count starts again
    take(first + 1)
    with pytest.raises(ProviderQuotaExceeded):
        take(first + 2)


def test_a_pause_refuses_until_its_instant_and_for_every_worker(engine: Engine) -> None:
    store = SqlBucketStore(engine)
    until = utc(2026, 11, 2, 8)
    store.suspend("googlebooks", until.timestamp())
    assert store.suspended_until("googlebooks", until.timestamp() - 1) == until.timestamp()
    other = SqlBucketStore(engine)  # another process reads the same database
    with pytest.raises(ProviderQuotaExceeded):
        other.reserve("googlebooks", 1000.0, 1000, 1000, until.timestamp() - 1)
    other.reserve("googlebooks", 1000.0, 1000, 1000, until.timestamp())  # midnight: it is over
    assert other.suspended_until("googlebooks", until.timestamp()) is None
    # another provider is not paused by it
    other.reserve("hardcover", 1000.0, 1000, 1000, until.timestamp() - 1)


def test_the_in_memory_bucket_counts_a_pacific_day_too() -> None:
    now = [utc(2026, 11, 1, 6, 59).timestamp()]
    bucket = TokenBucket(
        1000.0, 1000, daily_cap=1, wall=lambda: now[0], day_timezone=PACIFIC
    )
    bucket._reserve()
    with pytest.raises(ProviderQuotaExceeded):
        bucket._reserve()
    now[0] = utc(2026, 11, 1, 7).timestamp()
    bucket._reserve()
