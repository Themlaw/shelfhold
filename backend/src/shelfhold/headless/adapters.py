"""The production adapters of the two ports: one-shot calls through the guarded Fetcher.

Kavita: the exchange of the administrator's credentials for a named, expiring key is setup, not
operation: it uses two routes that are not on the allow-list of the client (`docs/kavita-routes.md`,
section "Routes de mise en place"). The account's own API key is never read or kept; the
password is sent once, in a JSON body, to the declared address only.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from shelfhold.export.targets.kavita.routes import CREATE_AUTH_KEY, LOGIN
from shelfhold.headless.errors import TargetExchangeFailed
from shelfhold.headless.ports import ExchangedKey
from shelfhold.infrastructure.fetcher import FetchError, GuardedFetcher

SERVER_INFO = "/api/Server/server-info-slim"
KEY_LENGTH = 32  # RotateAuthKeyRequestDto: 8 to 32


def _json(body: bytes) -> dict[str, Any]:
    try:
        loaded = json.loads(body)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


FetcherFactory = Callable[[str], GuardedFetcher]  # the address the admin gave -> its Fetcher


class HttpKavitaKeyExchange:
    def __init__(
        self, fetchers: FetcherFactory, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._fetchers = fetchers
        self._now = clock or (lambda: datetime.now(UTC))

    def exchange(
        self, *, url: str, username: str, password: str, key_name: str, expires_days: int
    ) -> ExchangedKey:
        try:
            fetcher = self._fetchers(url)
            return asyncio.run(
                self._exchange(fetcher, url.rstrip("/"), username, password, key_name, expires_days)
            )
        except FetchError:  # unreachable, refused by the SSRF guard or by the DNS blocklist
            raise TargetExchangeFailed("Kavita cannot be reached at this address") from None

    async def _exchange(
        self,
        fetcher: GuardedFetcher,
        base: str,
        username: str,
        password: str,
        key_name: str,
        expires_days: int,
    ) -> ExchangedKey:
        try:
            return await self._run(fetcher, base, username, password, key_name, expires_days)
        finally:
            await fetcher.aclose()

    async def _run(
        self,
        fetcher: GuardedFetcher,
        base: str,
        username: str,
        password: str,
        key_name: str,
        expires_days: int,
    ) -> ExchangedKey:
        login = await fetcher.post(
            base + LOGIN.path, json={"username": username, "password": password}
        )
        token = _json(login.body).get("token") if login.status == 200 else None
        if not isinstance(token, str) or not token:
            raise TargetExchangeFailed("Kavita refused these credentials")
        expires = self._now() + timedelta(days=expires_days)
        created = await fetcher.post(
            base + CREATE_AUTH_KEY.path,
            json={"name": key_name, "keyLength": KEY_LENGTH, "expiresUtc": expires.isoformat()},
            headers={"Authorization": f"Bearer {token}"},
        )
        document = _json(created.body)
        key = document.get("key") if created.status == 200 else None
        if not isinstance(key, str) or not key:
            raise TargetExchangeFailed("Kavita did not create the key (is the account an admin?)")
        info = await fetcher.get(base + SERVER_INFO, headers={"x-api-key": key})
        version = _json(info.body).get("kavitaVersion") if info.status == 200 else None
        raw = document.get("expiresAtUtc")
        try:
            when = datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else expires
        except ValueError:
            when = expires
        key_id = document.get("id")
        return ExchangedKey(
            key,
            when,
            version if isinstance(version, str) else None,
            key_id if isinstance(key_id, int) else None,
        )
