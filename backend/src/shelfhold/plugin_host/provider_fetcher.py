"""The Fetcher a metadata provider receives, and what the host keeps around it (ENF-LEG-005).

A provider never builds its own Fetcher and never limits itself: the host opens one per question
with the provider's declared domains, the identifying agent of the instance (with its contact when
it configured one), the provider's token bucket, the cache the provider cannot do without and
robots.txt. The bucket, the cache and the robots verdicts live in a `ProviderRuntime`, shared by
every Fetcher of the provider in the process: opening many Fetchers spends one quota.

The cache is the provider's condition, not the instance's option: `fetcher_cache` and
`fetcher_cache_off_sources` do not reach it. It never serves a request that carries credentials
(the Fetcher refuses to file one).
"""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from shelfhold.infrastructure.fetcher.cache import HttpCache
from shelfhold.infrastructure.fetcher.client import (
    FetchLimits,
    GuardedFetcher,
    identifiable_user_agent,
    is_identifiable,
)
from shelfhold.infrastructure.fetcher.errors import UserAgentNotIdentifiable
from shelfhold.infrastructure.fetcher.guard import DomainAllowlist
from shelfhold.infrastructure.fetcher.limiter import ProviderBuckets
from shelfhold.infrastructure.fetcher.resolver import Resolver
from shelfhold.infrastructure.fetcher.robots import RobotsPolicy
from shelfhold.sdk.fetcher import CookieJarView, DownloadedFile, Response
from shelfhold.sdk.metadata import MetadataManifest
from shelfhold.sdk.refs import ImageRequest

__all__ = [
    "DEFAULT_RUNTIME",
    "ProviderFetcher",
    "ProviderRuntime",
    "default_runtime",
    "open_provider",
]

_FORBIDDEN_HEADERS = frozenset({"user-agent", "host"})  # the host's, never the plugin's


class ProviderRuntime:
    """The state one provider keeps between questions: its bucket, its cache, robots.txt."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.buckets = ProviderBuckets(clock, sleep, wall)
        self.caches: dict[str, HttpCache] = {}
        self.robots: dict[str, RobotsPolicy] = {}

    def cache(self, provider: str) -> HttpCache:
        return self.caches.setdefault(provider, HttpCache())

    def robots_for(self, provider: str) -> RobotsPolicy:
        return self.robots.setdefault(provider, RobotsPolicy())


DEFAULT_RUNTIME = ProviderRuntime()


def default_runtime() -> ProviderRuntime:
    return DEFAULT_RUNTIME


def _clean(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {k: v for k, v in (headers or {}).items() if k.lower() not in _FORBIDDEN_HEADERS}


class ProviderFetcher:
    """The SDK `Fetcher` of one provider, over the guarded, polite, throttled one."""

    def __init__(self, inner: GuardedFetcher, manifest: MetadataManifest) -> None:
        if not is_identifiable(inner.user_agent):
            raise UserAgentNotIdentifiable("the agent of this Fetcher does not say who is asking")
        self._inner = inner
        self.manifest = manifest

    @property
    def user_agent(self) -> str:
        return self._inner.user_agent

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        cache_ttl: int | None = None,
    ) -> Response:
        return await self._inner.get(url, headers=_clean(headers), params=params, referer=referer)

    async def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return await self._inner.post(url, data=data, json=json, headers=_clean(headers))

    async def download(self, req: ImageRequest) -> DownloadedFile:
        """For a plugin that asks for a file itself: bounded like every download."""
        got = await self.fetch_image(req, max_bytes=_PLUGIN_DOWNLOAD_MAX)
        if got is None:
            return DownloadedFile(sha256="", size=0)
        import hashlib

        return DownloadedFile(sha256=hashlib.sha256(got).hexdigest(), size=len(got), content=got)

    def cookies(self) -> CookieJarView:
        return CookieJarView()

    async def fetch_image(self, req: ImageRequest, *, max_bytes: int) -> bytes | None:
        """The bytes a reference points to, or `None` (not a plain GET, or not a 2xx). Bytes of
        an unknown file: the caller decodes them in the worker and nowhere else."""
        if req.method != "GET" or req.body is not None:
            return None
        sink = io.BytesIO()
        done = await self._inner.download(
            req.url, sink, max_bytes=max_bytes, headers=_clean(req.headers), referer=req.referer
        )
        return sink.getvalue() if 200 <= done.status < 300 else None


_PLUGIN_DOWNLOAD_MAX = 16 * 1024 * 1024


def open_provider(
    manifest: MetadataManifest,
    resolver: Resolver,
    *,
    contact: str | None,
    runtime: ProviderRuntime | None = None,
    transport: Any = None,  # an httpx2 transport: a test's simulated server
    limits: FetchLimits | None = None,
) -> ProviderFetcher:
    """The Fetcher of `manifest`'s provider for this instance (`contact`: the configured one)."""
    runtime = runtime or default_runtime()
    inner = GuardedFetcher(
        resolver,
        limits=limits,
        transport=transport,
        user_agent=identifiable_user_agent(contact),
        domains=DomainAllowlist(manifest.key, manifest.permissions.domains),
        cache=runtime.cache(manifest.key),  # not optional
        robots=runtime.robots_for(manifest.key) if manifest.respects_robots else None,
        limiter=runtime.buckets.bucket_for(
            manifest.key, manifest.quota.effective(contact is not None)
        ),
    )
    return ProviderFetcher(inner, manifest)
