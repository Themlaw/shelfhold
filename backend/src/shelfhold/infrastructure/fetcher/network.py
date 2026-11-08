"""The one seam to the other servers of the owner (Kavita, Suwayomi).

`Network.integration_fetcher(url)` is the Fetcher for the address an admin typed: that exact host
and port is declared a LAN target (the name pinned to what it resolves to now); anything else
stays behind the SSRF guard. The resolver and the transport are the two things a test replaces
(a scripted DNS, a simulated server): the guard itself always runs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx2

from shelfhold.infrastructure.fetcher.client import FetchLimits, GuardedFetcher, IntegrationFetcher
from shelfhold.infrastructure.fetcher.errors import FetchError, InvalidLanTarget
from shelfhold.infrastructure.fetcher.lan import LanTargets
from shelfhold.infrastructure.fetcher.resolver import Resolver, SystemResolver
from shelfhold.sdk.fetcher import Response

__all__ = ["Network", "OneShotFetcher"]


@dataclass
class Network:
    resolver: Resolver = field(default_factory=SystemResolver)
    transport: httpx2.AsyncBaseTransport | None = None

    async def open_integration_fetcher(
        self, url: str, limits: FetchLimits | None = None
    ) -> GuardedFetcher:
        """`limits`: the delays of this service when they are not the default ones (the wake of a
        tool that sleeps waits longer than a request does)."""
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            lan = await LanTargets.declare([f"{parts.hostname}:{port}"], self.resolver)
        except (InvalidLanTarget, FetchError):  # not a LAN address, or blocked: stay guarded
            return GuardedFetcher(self.resolver, limits=limits, transport=self.transport)
        return IntegrationFetcher(
            self.resolver, lan_targets=lan, limits=limits, transport=self.transport
        )

    def integration_fetcher(self, url: str, limits: FetchLimits | None = None) -> GuardedFetcher:
        return asyncio.run(self.open_integration_fetcher(url, limits))

    def one_shot(self, url: str, limits: FetchLimits | None = None) -> OneShotFetcher:
        """A fetcher that opens its connection for each call and closes it: it is bound to no
        event loop, so one object serves the many short loops of the jobs."""
        return OneShotFetcher(self, url, limits)


class OneShotFetcher:
    def __init__(self, network: Network, url: str, limits: FetchLimits | None) -> None:
        self._network = network
        self._url = url
        self._limits = limits

    async def get(self, url: str, **options: Any) -> Response:
        fetcher = await self._network.open_integration_fetcher(self._url, self._limits)
        try:
            return await fetcher.get(url, **options)
        finally:
            await fetcher.aclose()
