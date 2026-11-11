"""The token bucket of a provider, and its daily ceiling (ENF-LEG-005).

The quota is declared in the manifest of the provider (`ProviderQuota`) and enforced here, by the
host, before every request that really leaves the machine: a plugin never limits itself, and
cannot spend more than its bucket allows by opening several Fetchers or by calling in a loop. A
cached answer costs no token. A burst above the bucket *waits*; the daily ceiling *refuses*.

There is no lock: a request reserves its token (the balance may go negative) in one step with no
`await` in it, then sleeps for what its reservation costs. Event loops come and go (a CLI call, a
job), so nothing here is bound to one.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from shelfhold.infrastructure.fetcher.errors import ProviderQuotaExceeded
from shelfhold.sdk.metadata import ProviderQuota

__all__ = ["BucketStore", "ProviderBuckets", "TokenBucket"]

_DAY_S = 86_400


class BucketStore(Protocol):
    """The shared balance of a provider (`provider_quota`): one atomic read-modify-write, so that
    two coroutines, two Fetchers and two processes spend one quota. Returns the seconds to wait
    before using the token it took; raises `ProviderQuotaExceeded` when the day is spent."""

    def reserve(
        self, provider: str, rate: float, burst: int, daily_cap: int | None, now: float
    ) -> float: ...


class TokenBucket:
    def __init__(
        self,
        rate: float,
        burst: int = 1,
        *,
        daily_cap: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall: Callable[[], float] = time.time,
        store: BucketStore | None = None,
        name: str = "",
    ) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("a bucket needs a positive rate and a burst of at least 1")
        self.rate = rate
        self.burst = burst
        self.daily_cap = daily_cap
        self._clock, self._sleep, self._wall = clock, sleep, wall
        self._tokens = float(burst)
        self._at = clock()
        self._day = int(wall() // _DAY_S)
        self._used = 0
        self._store, self._name = store, name

    def _reserve(self) -> float:
        """Take one token now; the seconds to wait before using it. Raises when the day is spent."""
        if self._store is not None:  # the database is the one balance
            return self._store.reserve(
                self._name, self.rate, self.burst, self.daily_cap, self._wall()
            )
        day = int(self._wall() // _DAY_S)
        if day != self._day:
            self._day, self._used = day, 0
        if self.daily_cap is not None and self._used >= self.daily_cap:
            raise ProviderQuotaExceeded(
                f"the daily ceiling of {self.daily_cap} requests is reached"
            )
        now = self._clock()
        self._tokens = min(float(self.burst), self._tokens + (now - self._at) * self.rate)
        self._at = now
        self._tokens -= 1.0
        self._used += 1
        return 0.0 if self._tokens >= 0 else -self._tokens / self.rate

    async def acquire(self) -> None:
        wait = self._reserve()
        if wait > 0:
            await self._sleep(wait)


@dataclass
class ProviderBuckets:
    """One bucket per provider, shared by every Fetcher of that provider; a new quota (a contact
    was configured) is a new bucket."""

    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    wall: Callable[[], float] = time.time
    store: BucketStore | None = None
    _buckets: dict[str, tuple[ProviderQuota, TokenBucket]] = field(default_factory=dict)

    def use_store(self, store: BucketStore) -> None:
        """Keep the balances in `store` from now on. Idempotent: the same store (the same
        database) changes nothing, so open buckets are never dropped."""
        if store != self.store:
            self.store = store
            self._buckets.clear()

    def bucket_for(self, provider: str, quota: ProviderQuota) -> TokenBucket:
        held = self._buckets.get(provider)
        if held is not None and held[0] == quota:
            return held[1]
        bucket = TokenBucket(
            quota.requests_per_second, quota.burst, daily_cap=quota.daily_cap,
            clock=self.clock, sleep=self.sleep, wall=self.wall, store=self.store, name=provider,
        )  # fmt: skip
        self._buckets[provider] = (quota, bucket)
        return bucket
