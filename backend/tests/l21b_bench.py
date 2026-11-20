"""What the BnF and Wikidata tests share: a clock nobody waits on, and a provider's own Fetcher
(guard, token bucket, cache, robots) over a fabricated server. Nothing leaves the machine."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from urllib.parse import parse_qs

from shelfhold_sdk.metadata import MetadataManifest

from shelfhold.plugin_host.provider_fetcher import ProviderFetcher, ProviderRuntime, open_provider
from tests.fakes.cassette_server import CassetteServer

EDITION = "01HZZZZZZZZZZZZZZZZZZZZZZA"


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


class FakeTime:
    """The bucket's clock and sleep: waiting moves the clock, nobody really waits."""

    def __init__(self) -> None:
        self.t = 10_000.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def worker(
    manifest: MetadataManifest,
    server: CassetteServer,
    time: FakeTime,
    runtime: ProviderRuntime | None = None,
) -> ProviderFetcher:
    """The Fetcher of one provider: `runtime` (bucket, cache) is shared when given."""
    server.clock = time.now
    runtime = runtime or ProviderRuntime(clock=time.now, sleep=time.sleep, wall=time.now)
    return open_provider(
        manifest, server.resolver, contact=None, runtime=runtime, transport=server.transport
    )


def asked(server: CassetteServer, index: int = -1) -> dict[str, str]:
    """The query parameters of the n-th request the fabricated server received."""
    call = server.calls()[index]
    return {k: v[0] for k, v in parse_qs(call.query, keep_blank_values=True).items()}


def busiest_second(server: CassetteServer) -> int:
    times = [c.at for c in server.calls()]
    return max(sum(1 for x in times if start <= x < start + 0.999) for start in times)


__all__ = ["EDITION", "FakeTime", "asked", "busiest_second", "run", "worker"]
