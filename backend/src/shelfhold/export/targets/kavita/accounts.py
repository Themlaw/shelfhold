"""A member's own Kavita key: the closed list of routes it may reach, and the check at link time.

The dedicated key (`credentials.AdminKey`) is the administrator's and reaches the allow-list of
`routes.py`. The key a member pastes (EF-USR-012, D8) is another thing: it lets Shelfhold ask
who the key is, once, when it is pasted, and read and write that member's reading progress (L14).
`MEMBER_ROUTES` is its whole reach, written here and in `docs/kavita-routes.md`; a call to anything
else is refused before a request is built, and the dedicated client refuses all of them in turn.

Every request goes through the `GuardedFetcher` of the target (declared LAN address, SSRF guard).
The key is a header and nothing else: never in a URL, a log line, an error or a `repr`.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.export.targets.kavita.client import CallResult
from shelfhold.export.targets.kavita.errors import ProtocolError, RouteNotAllowed, TargetError
from shelfhold.export.targets.kavita.routes import Route
from shelfhold.infrastructure.fetcher import FetchError, GuardedFetcher
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.schema import export

__all__ = [
    "CONTINUE_POINT",
    "GET_PROGRESS",
    "MEMBER_ROUTES",
    "POST_PROGRESS",
    "WHOAMI",
    "AccountCheckFailed",
    "DestinationInfo",
    "KavitaMemberVerifier",
    "MemberKavitaClient",
    "MemberKey",
    "same_account",
]

log = structlog.get_logger(__name__)

# `GET /api/Account` answers a `UserDto`: the name, and also a session token and the account's own
# default key. Only the name is read; the rest is dropped with the response and never kept.
WHOAMI: Final = Route("GET", "/api/Account", write=False)
GET_PROGRESS: Final = Route("GET", "/api/Reader/get-progress", write=False)
POST_PROGRESS: Final = Route("POST", "/api/Reader/progress", write=True)
CONTINUE_POINT: Final = Route("GET", "/api/Reader/continue-point", write=False)

MEMBER_ROUTES: Final = (WHOAMI, GET_PROGRESS, POST_PROGRESS, CONTINUE_POINT)
_BY_KEY: Final = {(r.method, r.path.lower()): r for r in MEMBER_ROUTES}
_MAX_NAME: Final = 128


class AccountCheckFailed(TargetError):
    """The key was not accepted. `code` is one of `token_refused` (Kavita refused it, or it opens
    another account: never said whose), `destination_unreachable` and `destination_not_supported`.
    The message holds the code and nothing else."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DestinationInfo:
    id: str
    is_kavita: bool


def _key(name: str) -> str:
    """Kavita's comparison of names: upper case, character by character (as the audit does)."""
    return "".join(c.upper() if len(c.upper()) == 1 else c for c in name)


def same_account(left: str, right: str) -> bool:
    return bool(left) and bool(right) and _key(left) == _key(right)


class MemberKey:
    """The key of one member's Kavita account. It does not print."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("an empty Kavita key")
        self._value = value
        register_secret_value(value)  # scrubbed by value from every later log line

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "MemberKey(<redacted>)"

    __str__ = __repr__


def _resolve(method: str, path: str) -> Route:
    route = _BY_KEY.get((method.upper(), path.lower()))
    if route is None:
        raise RouteNotAllowed(method, path)
    return route


class MemberKavitaClient:
    """One member's key against the routes of `MEMBER_ROUTES`, through the guarded fetcher."""

    def __init__(self, fetcher: GuardedFetcher, base_url: str, key: MemberKey) -> None:
        self._fetcher = fetcher
        self._base_url = base_url.rstrip("/")
        self._key = key

    def __repr__(self) -> str:
        return "MemberKavitaClient(<redacted>)"

    async def call(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> CallResult:
        route = _resolve(method, path)
        headers = {"x-api-key": self._key.reveal(), "Accept": "application/json"}
        url = self._base_url + route.path
        try:
            if route.method == "GET":
                response = await self._fetcher.get(
                    url, headers=headers, params=dict(query or {}) or None
                )
            else:
                response = await self._fetcher.post(
                    url, json=body if body is not None else {}, headers=headers
                )
        except FetchError as exc:
            log.warning("kavita.member_unreachable", route=route.label, reason=type(exc).__name__)
            raise ProtocolError(f"{route.label}: unreachable") from None
        log.debug("kavita.member_call", route=route.label, status=response.status)
        if response.status != 200:  # a 401 or 403 is Kavita refusing the key
            raise ProtocolError(f"{route.label} answered {response.status}", status=response.status)
        try:
            data = json.loads(response.body) if response.body else None
        except ValueError:
            raise ProtocolError(f"{route.label}: the answer is not JSON") from None
        return CallResult(response.status, data)

    async def whoami(self) -> str:
        """The name of the account the key opens. Nothing else of the answer leaves this method."""
        result = await self.call(WHOAMI.method, WHOAMI.path)
        data = result.data
        name = data.get("username") if isinstance(data, dict) else None
        if not isinstance(name, str) or not name or len(name) > _MAX_NAME:
            raise ProtocolError("GET /api/Account: no account name in the answer")
        return name


@dataclass(frozen=True)
class _Target:
    kind: str
    url: str | None


class KavitaMemberVerifier:
    """Checks, at link time, that a key opens the account the member declared.

    `fetcher_for(url)` is the guarded fetcher of the target (the host builds it: the address is
    declared, the SSRF guard applies at every hop). A key is sent nowhere else than to the address
    of the destination's own target, and nothing is kept of the answer but "yes" or a refusal.
    """

    def __init__(self, engine: Engine, fetcher_for: Callable[[str], GuardedFetcher]) -> None:
        self._engine = engine
        self._fetcher_for = fetcher_for

    def destination(self, destination_id: str) -> DestinationInfo | None:
        """The binding and what kind of server it points at, or `None` when there is none."""
        table, target = export.shelf_destination, export.export_target
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(table.c.id, target.c.kind)
                .join(target, target.c.id == table.c.target_id)
                .where(table.c.id == destination_id)
            ).first()
        return None if row is None else DestinationInfo(row.id, row.kind == "kavita")

    def _target(self, destination_id: str) -> _Target:
        table, target = export.shelf_destination, export.export_target
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(target.c.kind, target.c.config)
                .join(table, target.c.id == table.c.target_id)
                .where(table.c.id == destination_id)
            ).first()
        if row is None:
            return _Target("", None)
        config = row.config if isinstance(row.config, dict) else {}
        url = config.get("url")
        return _Target(row.kind, url if isinstance(url, str) else None)

    def verify(self, destination_id: str, remote_user: str, token: str) -> None:
        target = self._target(destination_id)
        if target.kind != "kavita" or not target.url or not urlsplit(target.url).hostname:
            raise AccountCheckFailed("destination_not_supported")
        client = MemberKavitaClient(self._fetcher_for(target.url), target.url, MemberKey(token))
        try:
            name = _run(client.whoami())
        except ProtocolError as exc:
            if exc.status in (401, 403):
                raise AccountCheckFailed("token_refused") from None
            raise AccountCheckFailed("destination_unreachable") from None
        if not same_account(name, remote_user):
            raise AccountCheckFailed(
                "token_refused"
            )  # a real key, of another account: not said whose


def _run(coro: Coroutine[Any, Any, str]) -> str:
    """Run a coroutine to its end from synchronous code, in a thread when a loop is already
    running in this one (a request handler runs in the pool of the server, not in a loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: list[str] = []
    failure: list[BaseException] = []

    def worker() -> None:
        try:
            box.append(asyncio.run(coro))
        except BaseException as exc:  # carried to the caller
            failure.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return box[0]
