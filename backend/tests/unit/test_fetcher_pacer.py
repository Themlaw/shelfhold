"""M2 L23 (ENF-LEG-003): the rhythm of a source is the host's, kept for the life of the process,
for every request that goes out, whoever asks and however many at once."""

from __future__ import annotations

import asyncio
import threading

import pytest

from shelfhold.infrastructure.fetcher.pacer import Pacer
from tests.gutenberg_env import FakeTime

pytestmark = pytest.mark.req("ENF-LEG-003")


def test_the_first_request_goes_at_once_and_the_next_waits_the_interval() -> None:
    t = FakeTime()
    pacer = Pacer(5.0, clock=t.clock, sleep=t.sleep)
    for _ in range(3):
        asyncio.run(pacer.wait())
    assert t.slept == [5.0, 5.0]


def test_time_already_spent_is_not_waited_twice() -> None:
    t = FakeTime()
    pacer = Pacer(5.0, clock=t.clock, sleep=t.sleep)
    asyncio.run(pacer.wait())
    t.now += 3
    asyncio.run(pacer.wait())
    t.now += 30
    asyncio.run(pacer.wait())
    assert t.slept == [2.0]


def test_requests_made_at_the_same_time_are_spread_one_interval_apart() -> None:
    t = FakeTime()
    pacer = Pacer(2.0, clock=t.clock, sleep=lambda s: asyncio.sleep(0))
    delays: list[float] = []

    async def one() -> None:
        delays.append(pacer.reserve())

    async def many() -> None:
        await asyncio.gather(*(one() for _ in range(5)))

    asyncio.run(many())
    assert sorted(delays) == [0.0, 2.0, 4.0, 6.0, 8.0]


def test_threads_cannot_take_the_same_slot() -> None:
    t = FakeTime()
    pacer = Pacer(1.0, clock=t.clock, sleep=t.sleep)
    taken: list[float] = []
    lock = threading.Lock()

    def grab() -> None:
        delay = pacer.reserve()
        with lock:
            taken.append(delay)

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(taken) == [float(i) for i in range(16)]


@pytest.mark.parametrize("rate", [0.0, -1.0, float("nan"), float("inf")])
def test_a_rate_that_is_not_positive_and_finite_is_refused(rate: float) -> None:
    with pytest.raises(ValueError, match="rate"):
        Pacer.for_rate(rate)


def test_a_clock_that_goes_back_never_shortens_the_wait() -> None:
    t = FakeTime()
    pacer = Pacer(5.0, clock=t.clock, sleep=t.sleep)
    pacer.reserve()
    t.now -= 100  # a wall clock set back
    assert pacer.reserve() >= 5.0
