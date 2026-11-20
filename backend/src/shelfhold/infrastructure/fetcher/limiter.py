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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from shelfhold_sdk.metadata import AdaptiveLimit, ProviderQuota

from shelfhold.infrastructure.fetcher.day_window import day_key
from shelfhold.infrastructure.fetcher.errors import ProviderQuotaExceeded

__all__ = ["BucketStore", "ProviderBuckets", "TokenBucket"]

_DAY_S = 86_400


class BucketStore(Protocol):
    """The shared balance of a provider (`provider_quota`): one atomic read-modify-write, so that
    two coroutines, two Fetchers and two processes spend one quota. Returns the seconds to wait
    before using the token it took; raises `ProviderQuotaExceeded` when the day is spent."""

    def reserve(
        self,
        provider: str,
        rate: float,
        burst: int,
        daily_cap: int | None,
        now: float,
        adaptive: AdaptiveLimit | None = None,
        day_timezone: str | None = None,
    ) -> float: ...

    def announce(self, provider: str, limit: int) -> None: ...

    def defer(self, provider: str, seconds: float, now: float) -> None: ...

    def suspend(self, provider: str, until: float) -> None: ...

    def suspended_until(self, provider: str, now: float) -> float | None: ...


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
        adaptive: AdaptiveLimit | None = None,
        day_timezone: str | None = None,
    ) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("a bucket needs a positive rate and a burst of at least 1")
        self.rate = rate
        self.burst = burst
        self.daily_cap = daily_cap
        self._clock, self._sleep, self._wall = clock, sleep, wall
        self._tokens = float(burst)
        self._at = clock()
        self.day_timezone = day_timezone
        self._day = day_key(wall(), day_timezone)
        self._used = 0
        self._store, self._name = store, name
        self.adaptive = adaptive
        self._announced: int | None = None
        if adaptive is not None:
            self.rate = adaptive.start_per_minute / 60  # until the provider says otherwise

    def _reserve(self) -> float:
        """Take one token now; the seconds to wait before using it. Raises when the day is spent."""
        if self._store is not None:  # the database is the one balance
            return self._store.reserve(
                self._name, self.rate, self.burst, self.daily_cap, self._wall(),
                self.adaptive, self.day_timezone,
            )  # fmt: skip
        day = day_key(self._wall(), self.day_timezone)
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

    def _settle(self) -> None:
        now = self._clock()
        self._tokens = min(float(self.burst), self._tokens + (now - self._at) * self.rate)
        self._at = now

    def observe(self, headers: Mapping[str, str], status: int) -> None:
        """What a response says about the rate: the limit the provider announces (never above the
        ceiling of the manifest), and a 429's `Retry-After` (the next request waits for it)."""
        lowered = {k.lower(): v for k, v in headers.items()}
        if self.adaptive is not None:
            value = lowered.get(self.adaptive.header.lower(), "")
            if value.isdigit() and int(value) > 0:
                limit = min(self.adaptive.max_per_minute, int(value))
                self._announced = limit
                self._settle()  # what elapsed so far was earned at the old rate, not the new
                self.rate = limit / 60
                if self._store is not None:
                    self._store.announce(self._name, limit)
        if status == 429:
            after = lowered.get("retry-after", "")
            seconds = min(float(after), _DAY_S) if after.replace(".", "", 1).isdigit() else 60.0
            if self._store is not None:
                self._store.defer(self._name, seconds, self._wall())
            else:
                self._tokens = -seconds * self.rate
                self._at = self._clock()

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
            adaptive=quota.adaptive, day_timezone=quota.day_timezone,
        )  # fmt: skip
        self._buckets[provider] = (quota, bucket)
        return bucket
