"""The rhythm of a source: one slot every `1 / rate` seconds, for every request that goes out
(ENF-LEG-003).

It belongs to the host and lives as long as the process, one per source: whoever asks, however many
Fetchers are built (one is built per operation), however many requests are made at once, they take
slots from the same counter. `reserve` hands out the next free slot under a thread lock and returns
how long to wait for it, so that two requests made at the same time cannot take the same one, and a
clock that goes back never shortens a wait. Nothing here is persisted: a restart starts a new
rhythm (the first request goes at once), which the scheduler's jitter and the cache soften.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Awaitable, Callable

__all__ = ["Pacer"]


class Pacer:
    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if not (math.isfinite(interval) and interval > 0):
            raise ValueError("the interval between two requests must be positive and finite")
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = float("-inf")  # the first request goes at once

    @classmethod
    def for_rate(
        cls,
        requests_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> Pacer:
        if not (math.isfinite(requests_per_second) and requests_per_second > 0):
            raise ValueError("a rate must be positive and finite")
        return cls(1.0 / requests_per_second, clock=clock, sleep=sleep)

    def reserve(self) -> float:
        """Take the next slot; the seconds to wait before using it."""
        with self._lock:
            now = self._clock()
            if self._next > now + self._interval * 1e6:  # a clock set far back: start over
                self._next = float("-inf")
            slot = max(now, self._next)
            self._next = slot + self._interval
            return slot - now

    async def wait(self) -> None:
        delay = self.reserve()
        if delay > 0:
            await self._sleep(delay)

    async def acquire(self) -> None:
        """The `RequestLimiter` of the Fetcher: taken before each request that really leaves."""
        await self.wait()
