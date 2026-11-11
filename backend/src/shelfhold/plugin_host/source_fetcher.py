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
  credential, or a response that sets a cookie, is never cached, and one source never reads the
  entry of another.

The cache and the robots rules are kept for the life of the process, per source: the Fetcher
itself is built for each operation, because its connection pool belongs to one event loop.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

from shelfhold.infrastructure.fetcher import FetchLimits, GuardedFetcher, Network
from shelfhold.infrastructure.fetcher.cache import HttpCache, cache_for
from shelfhold.infrastructure.fetcher.guard import DomainAllowlist
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
    ) -> None:
        self._network = network
        self._enabled = cache_enabled
        self._off = frozenset(cache_off_sources)
        self._limits = limits
        self._caches: dict[str, HttpCache | None] = {}
        self._robots: dict[str, RobotsPolicy] = {}

    def cache_of(self, key: str) -> HttpCache | None:
        if key not in self._caches:
            self._caches[key] = cache_for(key, self._enabled, self._off)
        return self._caches[key]

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
        )
