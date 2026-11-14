"""The production adapters of the two ports: one-shot calls through the guarded Fetcher.

Kavita: the exchange of the administrator's credentials for a named, expiring key is setup, not
operation: it uses two routes that are not on the allow-list of the client (`docs/kavita-routes.md`,
section "Routes de mise en place"). The account's own API key is never read or kept; the
password is sent once, in a JSON body, to the declared address only.
"""

from __future__ import annotations

import asyncio
import json
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from shelfhold.export.targets.kavita.routes import (
    CREATE_AUTH_KEY,
    DELETE_AUTH_KEY,
    LIST_AUTH_KEYS,
    LOGIN,
)
from shelfhold.headless.errors import TargetExchangeFailed
from shelfhold.headless.ports import ExchangedKey
from shelfhold.infrastructure.fetcher import FetchError, GuardedFetcher

SERVER_INFO = "/api/Server/server-info-slim"
KEY_LENGTH = 32  # RotateAuthKeyRequestDto: 8 to 32


def key_digest(key: str) -> str:
    """What is written about a key instead of its value: a 32-bit checksum. It only has to tell
    "the same key" from "another key that got the same identifier" (an accident, not an
    attacker), and it reveals nothing usable of a random token."""
    return f"{zlib.crc32(key.encode()):08x}"


class _KeyIdReused(Exception):
    """The identifier on record now names a key whose value is not the sealed one."""


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

    def revoke(self, *, url: str, username: str, password: str, key: str, key_name: str) -> bool:
        try:
            fetcher = self._fetchers(url)
            return asyncio.run(
                self._revoke(fetcher, url.rstrip("/"), username, password, key, key_name)
            )
        except FetchError:
            raise TargetExchangeFailed("Kavita cannot be reached at this address") from None

    def find_key_id(
        self, *, url: str, username: str, password: str, key: str
    ) -> tuple[str, str] | None:
        try:
            fetcher = self._fetchers(url)
            found = asyncio.run(self._keys(fetcher, url.rstrip("/"), username, password, None))
        except FetchError:
            raise TargetExchangeFailed("Kavita cannot be reached at this address") from None
        row = next((i for i in found if i.get("key") == key), None)
        return None if row is None else (str(row["id"]), key_digest(key))

    def revoke_key_id(
        self, *, url: str, username: str, password: str, key_id: str, key_digest: str | None = None
    ) -> str:
        try:
            fetcher = self._fetchers(url)
            asked = asyncio.run(
                self._keys(fetcher, url.rstrip("/"), username, password, key_id, key_digest)
            )
        except FetchError:
            raise TargetExchangeFailed("Kavita cannot be reached at this address") from None
        except _KeyIdReused:
            return "reused"
        return "revoked" if asked == [] else "failed"

    async def _keys(
        self,
        fetcher: GuardedFetcher,
        base: str,
        username: str,
        password: str,
        drop: str | None,
        digest: str | None = None,
    ) -> list[dict[str, Any]]:
        """Log in and list the account's keys; with `drop`, delete that identifier and only it
        (nothing is deleted when it is not in the list, or when `digest` is given and the key of
        that identifier no longer has this digest). Returns the rows (empty after a deletion that
        went through, or when the identifier was already gone)."""
        try:
            login = await fetcher.post(
                base + LOGIN.path, json={"username": username, "password": password}
            )
            token = _json(login.body).get("token") if login.status == 200 else None
            if not isinstance(token, str) or not token:
                raise TargetExchangeFailed("Kavita refused these credentials")
            auth = {"Authorization": f"Bearer {token}"}
            listed = await fetcher.get(base + LIST_AUTH_KEYS.path, headers=auth)
            try:
                items = json.loads(listed.body) if listed.status == 200 else []
            except ValueError:
                items = []
            if listed.status != 200:
                raise TargetExchangeFailed("Kavita did not list the keys")
            rows = [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), int)]
            if drop is None:
                return rows
            row = next((i for i in rows if str(i["id"]) == drop), None)
            if row is None:
                return []  # already gone
            if digest is not None and key_digest(str(row.get("key", ""))) != digest:
                raise _KeyIdReused  # the identifier now names another key: it is not ours to delete
            gone = await fetcher.delete(
                base + DELETE_AUTH_KEY.path, headers=auth, params={"authKeyId": int(drop)}
            )
            return [] if 200 <= gone.status < 300 else rows
        finally:
            await fetcher.aclose()

    async def _revoke(
        self, fetcher: GuardedFetcher, base: str, username: str, password: str, key: str, name: str
    ) -> bool:
        try:
            login = await fetcher.post(
                base + LOGIN.path, json={"username": username, "password": password}
            )
            token = _json(login.body).get("token") if login.status == 200 else None
            if not isinstance(token, str) or not token:
                raise TargetExchangeFailed("Kavita refused these credentials")
            auth = {"Authorization": f"Bearer {token}"}
            listed = await fetcher.get(base + LIST_AUTH_KEYS.path, headers=auth)
            try:
                items = json.loads(listed.body) if listed.status == 200 else []
            except ValueError:
                items = []
            rows = [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), int)]
            found = next((i for i in rows if i.get("key") == key), None) or next(
                (i for i in rows if name and i.get("name") == name), None
            )
            if found is None:
                return False
            gone = await fetcher.delete(
                base + DELETE_AUTH_KEY.path, headers=auth, params={"authKeyId": found["id"]}
            )
            return 200 <= gone.status < 300
        finally:
            await fetcher.aclose()

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
