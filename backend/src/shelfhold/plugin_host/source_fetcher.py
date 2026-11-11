"""The Fetcher of one source plugin: its DomainRules, robots.txt and the HTTP cache (EF-SRC-003,
ENF-LEG-007).

Every request a source makes, for a search, a pasted URL or a file, goes through the Fetcher built
here and through no other. It carries three things the plugin cannot take off:

- the plugin's `DomainRule`s, checked by name at every hop, so a reference or a redirection that
  leaves them is refused before any resolution;
- `robots.txt` of each origin it reaches, for every source that is not a connected service (a
  connected service is a tool the owner installed, not a site; the manifest's `respects_robots`
  can only lower it for a connected service);
- the HTTP cache of that source alone (`cache_for`, `fetcher_cache`): a request that carries a
  credential is never cached, a response that sets a cookie is never cached, and the client keeps
  no cookie (its jar refuses every domain), so no request carries one the caller did not write;
  one source never reads the entry of another.

- the rhythm of the source (`rate_limit` of its manifest, ENF-LEG-003): one `Pacer` per source,
  shared by every Fetcher built for it, so that a request made by another operation, at the same
  time as another or after a redirection still waits for its slot. A connected service has its own
  queue and its own rhythm (EF-SRC-027), and is not paced here.

The cache, the robots rules and the pacer are kept for the life of the process, per source: the
Fetcher itself is built for each operation, because its connection pool belongs to one event loop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import TYPE_CHECKING, Any

from shelfhold.infrastructure.fetcher import FetchLimits, GuardedFetcher, Network
from shelfhold.infrastructure.fetcher.cache import HttpCache, cache_for
from shelfhold.infrastructure.fetcher.guard import DomainAllowlist
from shelfhold.infrastructure.fetcher.pacer import Pacer
from shelfhold.infrastructure.fetcher.robots import RobotsPolicy

if TYPE_CHECKING:
    from shelfhold.plugin_host.registry import PluginRecord

__all__ = ["SourceFetchers"]


def _is_connected_service(instance: Any) -> bool:
    return getattr(instance, "connected", None) is not None


class SourceFetchers:
    def __init__(
        self,
        network: Network,
        *,
        cache_enabled: bool = True,
        cache_off_sources: Collection[str] = (),
        limits: FetchLimits | None = None,
        pace_clock: Callable[[], float] = time.monotonic,
        pace_sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._network = network
        self._pace_clock = pace_clock
        self._pace_sleep = pace_sleep
        self._pacers: dict[str, Pacer] = {}
        self._enabled = cache_enabled
        self._off = frozenset(cache_off_sources)
        self._limits = limits
        self._caches: dict[str, HttpCache | None] = {}
        self._robots: dict[str, RobotsPolicy] = {}

    def cache_of(self, key: str) -> HttpCache | None:
        if key not in self._caches:
            self._caches[key] = cache_for(key, self._enabled, self._off)
        return self._caches[key]

    def pacer_of(self, record: PluginRecord) -> Pacer | None:
        """The rhythm of a native source, from its manifest; none for a connected service."""
        manifest = record.manifest
        rate = getattr(manifest, "rate_limit", None)
        if rate is None or _is_connected_service(record.instance):
            return None
        if record.key not in self._pacers:
            self._pacers[record.key] = Pacer.for_rate(
                rate.requests_per_second, clock=self._pace_clock, sleep=self._pace_sleep
            )
        return self._pacers[record.key]

    def reads_robots(self, record: PluginRecord) -> bool:
        manifest = record.manifest
        if manifest is None:
            return False
        if _is_connected_service(record.instance):
            return bool(manifest.respects_robots)
        return True  # a native source reads robots.txt whatever its manifest says

    def allowlist(
        self, record: PluginRecord, settings: Mapping[str, object] | None = None
    ) -> DomainAllowlist:
        manifest = record.manifest
        rules = manifest.permissions.domains if manifest is not None else []
        return DomainAllowlist(record.key, rules, settings)

    def for_record(
        self, record: PluginRecord, settings: Mapping[str, object] | None = None
    ) -> GuardedFetcher:
        robots = (
            self._robots.setdefault(record.key, RobotsPolicy())
            if self.reads_robots(record)
            else None
        )
        return GuardedFetcher(
            self._network.resolver,
            limits=self._limits,
            transport=self._network.transport,
            domains=self.allowlist(record, settings),
            cache=self.cache_of(record.key),
            robots=robots,
            limiter=self.pacer_of(record),
            settings=settings,
        )
