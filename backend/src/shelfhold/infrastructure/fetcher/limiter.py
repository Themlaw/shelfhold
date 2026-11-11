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
    """Where a bucket keeps its balance between two processes (`provider_quota`): a command run
    by itself does not start with a full bucket. `at` is a wall-clock instant at which the balance
    (never below 0) is `tokens`; a debt is a balance that reaches 0 in the future."""

    def load(self, provider: str) -> tuple[float, float] | None: ...

    def save(self, provider: str, tokens: float, at: float) -> None: ...


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
        kept = store.load(name) if store is not None else None
        if kept is not None:  # what the previous process left, refilled by the time since
            tokens, at = kept
            self._tokens = min(float(burst), tokens + (wall() - at) * rate)

    def _reserve(self) -> float:
        """Take one token now; the seconds to wait before using it. Raises when the day is spent."""
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
        wait = 0.0 if self._tokens >= 0 else -self._tokens / self.rate
        if self._store is not None:
            self._store.save(self._name, max(self._tokens, 0.0), self._wall() + wait)
        return wait

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
        """Keep the balances in `store` from now on (buckets already open are reopened from it)."""
        if store is not self.store:
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
