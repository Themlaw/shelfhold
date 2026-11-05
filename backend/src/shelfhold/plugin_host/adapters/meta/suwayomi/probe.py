"""The authentication-mode probe behind `shelfhold source add suwayomi` (EF-SRC-031).

It lives in the adapter package because the core never names a Suwayomi symbol (EF-SRC-033); the
composition of the headless configuration imports this one module, and the import contract says so.
One request through the guarded Fetcher, the credentials (if any) injected by the host wrapper.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

from shelfhold.infrastructure.fetcher import FetchError, GuardedFetcher
from shelfhold.plugin_host.adapters.meta.suwayomi.service import SuwayomiService
from shelfhold.plugin_host.authflow import AuthInjectingFetcher
from shelfhold.sdk.models import AuthBasic
from shelfhold.sdk.source import AuthRequired

FetcherFactory = Callable[[str], GuardedFetcher]


class HttpSuwayomiProbe:
    def __init__(self, fetchers: FetcherFactory) -> None:
        self._fetchers = fetchers

    def auth_mode(self, *, url: str, username: str | None, password: str | None) -> str:
        try:
            fetcher = self._fetchers(url)
            return asyncio.run(self._mode(fetcher, url, username, password))
        except FetchError:
            raise ConnectionError("unreachable") from None  # the port's way to say so

    async def _mode(
        self, inner: GuardedFetcher, url: str, username: str | None, password: str | None
    ) -> str:
        fetcher: Any = inner  # a `Fetcher` of the plugin SDK, as the service expects
        if username and password:
            fetcher = AuthInjectingFetcher(
                cast("Any", inner),
                AuthBasic(),
                {"username": username, "password": password},
                origin=url,
            )
        try:
            return await SuwayomiService(url).auth_mode(fetcher)
        except AuthRequired:
            return "basic_auth"  # a 401 without (or despite) credentials: Basic is what it asks
        finally:
            await inner.aclose()
