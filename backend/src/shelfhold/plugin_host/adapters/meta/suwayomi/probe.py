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
from shelfhold.plugin_host.adapters.meta.suwayomi.jwt import suwayomi_jwt_fetcher
from shelfhold.plugin_host.adapters.meta.suwayomi.service import SuwayomiService
from shelfhold.plugin_host.authflow import AuthInjectingFetcher
from shelfhold.plugin_host.errors import AuthExpired, AuthModeUnsupported
from shelfhold.sdk.models import AuthBasic
from shelfhold.sdk.source import AuthRequired

FetcherFactory = Callable[[str], GuardedFetcher]


class HttpSuwayomiProbe:
    def __init__(self, fetchers: FetcherFactory) -> None:
        self._fetchers = fetchers
        # `ui_login`: were the roles of the account read and found harmless? False when the
        # server does not expose them (the admin is warned, D1). Set by each `auth_mode` call.
        self.roles_verified = True
        # EF-SRC-026 (d): the global update settings of the server that would leave out the
        # series Shelfhold follows (names); empty when none, or when the server does not say.
        self.blocking_filters: list[str] = []

    def auth_mode(self, *, url: str, username: str | None, password: str | None) -> str:
        try:
            fetcher = self._fetchers(url)
            self.roles_verified = True
            self.blocking_filters = []
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
            try:
                mode = await SuwayomiService(url).auth_mode(fetcher)
            except AuthRequired:
                return "basic_auth"  # a 401 without (or despite) credentials: Basic is what it asks
            if mode == "ui_login" and username and password:
                reader: Any = await self._check_service_account(inner, url, username, password)
                await self._read_filters(reader, url)
            elif mode == "none" or (mode == "basic_auth" and username and password):
                await self._read_filters(fetcher, url)
            return mode
        finally:
            await inner.aclose()

    async def _read_filters(self, reader: Any, url: str) -> None:
        """What the server would not refresh: only read, and an answer that cannot be understood
        is not a reason to refuse the source (the next probe asks again)."""
        try:
            self.blocking_filters = await SuwayomiService(url).blocking_update_filters(reader)
        except Exception:
            self.blocking_filters = []

    async def _check_service_account(
        self, inner: GuardedFetcher, url: str, username: str, password: str
    ) -> Any:
        """`ui_login` (EF-SRC-031): the account logs in, and, when the server shows the roles, is
        not one that may install extensions (D1). `PermissionError`: the server refuses the
        account. `ValueError`: the account is too powerful (the message says why)."""
        jwt = suwayomi_jwt_fetcher(
            cast("Any", inner), {"username": username, "password": password}, origin=url
        )
        try:
            self.roles_verified = await SuwayomiService(url).ensure_least_privilege(
                cast("Any", jwt)
            )
            return jwt
        except AuthExpired:
            raise PermissionError("the server refuses this account") from None
        except AuthModeUnsupported as too_powerful:
            raise ValueError(str(too_powerful)) from None
