"""EF-INT-003, ENF-OPS-007: per-domain concurrency slots in `domain_slot`."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata
from shelfhold.jobs.slots import DomainSlots, UnknownLimitKeyError
from tests.fakes_clock import ManualClock


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "slots.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


class SlotBusyError(Exception):
    pass


@contextmanager
def held(slots: DomainSlots, limit_key: str) -> Iterator[None]:
    """A slot for the time of a block (production takes and gives it back in two steps)."""
    if not slots.try_acquire(limit_key):
        raise SlotBusyError(limit_key)
    try:
        yield
    finally:
        slots.release(limit_key)


@pytest.mark.req("EF-INT-003")
def test_domain_slot_caps_concurrency(engine: Engine) -> None:
    slots = DomainSlots(engine, ManualClock(NOW))
    slots.declare("example.org", 2)
    assert slots.try_acquire("example.org")
    assert slots.try_acquire("example.org")
    assert not slots.try_acquire("example.org")
    slots.release("example.org")
    assert slots.try_acquire("example.org")
    with pytest.raises(UnknownLimitKeyError):
        slots.try_acquire("other.example")


@pytest.mark.req("EF-INT-003")
def test_domain_slot_under_parallel_acquirers(engine: Engine) -> None:
    slots = DomainSlots(engine, ManualClock(NOW))
    slots.declare("example.org", 2)
    barrier = Barrier(8)

    def grab() -> bool:
        barrier.wait()
        return slots.try_acquire("example.org")

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: grab(), range(8)))
    assert results.count(True) == 2


@pytest.mark.req("EF-INT-003")
def test_slot_taken_then_released_reports_busy(engine: Engine) -> None:
    slots = DomainSlots(engine, ManualClock(NOW))
    slots.declare("d", 1)
    with held(slots, "d"), pytest.raises(SlotBusyError), held(slots, "d"):
        pass
    with held(slots, "d"):
        pass
    assert slots.in_use("d") == 0


@pytest.mark.req("EF-INT-003", "ENF-OPS-007")
def test_expired_lease_is_reclaimed_and_boot_releases_all(engine: Engine) -> None:
    clock = ManualClock(NOW)
    slots = DomainSlots(engine, clock, lease=timedelta(minutes=10))
    slots.declare("d", 1)
    assert slots.try_acquire("d")
    clock.advance(minutes=11)  # the holder died without releasing
    assert slots.try_acquire("d")
    assert slots.in_use("d") == 1
    assert slots.release_all() == 1
    assert slots.in_use("d") == 0
    slots.declare("d", 3)  # re-declaring changes the cap, keeps the leases
    assert slots.try_acquire("d")
    slots.declare("d", 3)
    assert slots.in_use("d") == 1
