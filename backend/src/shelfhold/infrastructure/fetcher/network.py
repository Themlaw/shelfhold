"""The one seam to the other servers of the owner (Kavita, Suwayomi).

`Network.integration_fetcher(url)` is the Fetcher for the address an admin typed: that exact host
and port is declared a LAN target (the name pinned to what it resolves to now); anything else
stays behind the SSRF guard. The resolver and the transport are the two things a test replaces
(a scripted DNS, a simulated server): the guard itself always runs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx2

from shelfhold.infrastructure.fetcher.client import GuardedFetcher, IntegrationFetcher
from shelfhold.infrastructure.fetcher.errors import FetchError, InvalidLanTarget
from shelfhold.infrastructure.fetcher.lan import LanTargets
from shelfhold.infrastructure.fetcher.resolver import Resolver, SystemResolver

__all__ = ["Network"]


@dataclass
class Network:
    resolver: Resolver = field(default_factory=SystemResolver)
    transport: httpx2.AsyncBaseTransport | None = None

    def integration_fetcher(self, url: str) -> GuardedFetcher:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            lan = asyncio.run(LanTargets.declare([f"{parts.hostname}:{port}"], self.resolver))
        except (InvalidLanTarget, FetchError):  # not a LAN address, or blocked: stay guarded
            return GuardedFetcher(self.resolver, transport=self.transport)
        return IntegrationFetcher(self.resolver, lan_targets=lan, transport=self.transport)
