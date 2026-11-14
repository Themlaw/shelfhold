"""The answers of a metadata provider, kept in `metadata_cache` (ENF-LEG-005, ENF-LEG-007).

The cache is mandatory and its life is the host's: `METADATA_CACHE_TTL` from the moment the answer
was fetched, whatever the upstream says (no `Cache-Control`, or `no-cache`, changes nothing), and
it outlives the process. Only a 200 to a GET that carries no credential is kept; a hit costs no
request and no token. The key is a digest of the address, never the address.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fetcher.cache import cacheable_request
from shelfhold.infrastructure.schema import platform
from shelfhold.sdk.fetcher import CookieJarView, DownloadedFile, Response
from shelfhold.sdk.refs import ImageRequest

__all__ = ["METADATA_CACHE_TTL", "CachingFetcher", "PayloadCache"]

METADATA_CACHE_TTL: Final = timedelta(hours=24)
_MAX_BODY: Final = 1024 * 1024
_KEPT_HEADERS: Final = ("content-type",)


class PayloadCache:
    def __init__(self, engine: Engine, clock: Clock, ttl: timedelta = METADATA_CACHE_TTL) -> None:
        self._engine, self._clock, self._ttl = engine, clock, ttl

    @staticmethod
    def _hash(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def get(self, provider: str, url: str) -> Response | None:
        table = platform.metadata_cache
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(table.c.payload, table.c.expires_at).where(
                    table.c.provider == provider, table.c.key_hash == self._hash(url)
                )
            ).first()
        if row is None or row.expires_at.timestamp() <= self._clock.now().timestamp():
            return None
        payload = row.payload
        return Response(
            status=int(payload["status"]), headers=dict(payload["headers"]),
            body=base64.b64decode(payload["body"]),
        )  # fmt: skip

    def put(self, provider: str, url: str, response: Response) -> None:
        if response.status != 200 or len(response.body) > _MAX_BODY:
            return
        now = self._clock.now()
        payload: dict[str, Any] = {
            "status": 200,
            "headers": {k: v for k, v in response.headers.items() if k.lower() in _KEPT_HEADERS},
            "body": base64.b64encode(response.body).decode(),
        }
        values = {"payload": payload, "fetched_at": now, "expires_at": now + self._ttl}
        table = platform.metadata_cache
        with write_transaction(self._engine) as connection:
            connection.execute(sa.delete(table).where(table.c.expires_at <= now))
            connection.execute(
                sqlite_insert(table)
                .values(provider=provider, key_hash=self._hash(url), **values)
                .on_conflict_do_update(
                    index_elements=[table.c.provider, table.c.key_hash], set_=values
                )
            )


class CachingFetcher:
    """The Fetcher a provider is given: its reads go through `PayloadCache` first."""

    def __init__(self, inner: Any, cache: PayloadCache, provider: str) -> None:
        self._inner, self._cache, self._provider = inner, cache, provider

    @property
    def user_agent(self) -> str:
        return str(self._inner.user_agent)

    async def fetch_image(self, req: ImageRequest, *, max_bytes: int) -> bytes | None:
        got: bytes | None = await self._inner.fetch_image(req, max_bytes=max_bytes)
        return got

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        cache_ttl: int | None = None,
    ) -> Response:
        keep = not params and cacheable_request(headers or {})
        if keep and (hit := self._cache.get(self._provider, url)) is not None:
            return hit
        response: Response = await self._inner.get(
            url, headers=headers, params=params, referer=referer, cache_ttl=cache_ttl
        )
        if keep:
            self._cache.put(self._provider, url, response)
        return response

    async def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        resp: Response = await self._inner.post(url, data=data, json=json, headers=headers)
        return resp

    async def download(self, req: ImageRequest) -> DownloadedFile:
        done: DownloadedFile = await self._inner.download(req)
        return done

    def cookies(self) -> CookieJarView:
        return CookieJarView()

    def settings(self) -> dict[str, Any]:
        return {}  # a provider's Fetcher carries no per-source settings
