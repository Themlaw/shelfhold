"""EF-INT-003, ENF-OPS-007: per-domain concurrency slots in `domain_slot`."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata
from shelfhold.jobs.slots import DomainSlots, SlotBusyError, UnknownLimitKeyError
from tests.fakes_clock import ManualClock


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "slots.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


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
def test_slot_context_manager_releases_and_reports_busy(engine: Engine) -> None:
    slots = DomainSlots(engine, ManualClock(NOW))
    slots.declare("d", 1)
    with slots.slot("d"):
        with pytest.raises(SlotBusyError), slots.slot("d"):
            pass
    with slots.slot("d"):
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
