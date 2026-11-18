"""A member's own Kavita key: the closed list of routes it may reach, and the check at link time.

The dedicated key (`credentials.AdminKey`) is the administrator's and reaches the allow-list of
`routes.py`. The key a member pastes (EF-USR-012, D8) is another thing: it lets Shelfhold ask
who the key is, once, when it is pasted, and read and write that member's reading progress (L14).
`MEMBER_ROUTES` is its whole reach, written here and in `docs/kavita-routes.md`; a call to anything
else is refused before a request is built, and the dedicated client refuses all of them in turn.

Every request goes through the `GuardedFetcher` of the target (declared LAN address, SSRF guard).
The key is a header and nothing else: never in a URL, a log line, an error or a `repr`. **One
exception** (M5 L09): the two kosync routes of Kavita read the key of the account in their path
(`/api/Koreader/{apiKey}/...`), so it is put there by this client from its own key, never by the
caller, and sent in no header; the call is logged under the template of the route, and the key
is registered to be masked by value (and by shape) in every other line that names the path.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlencode, urlsplit

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.export.domain.kavita_child_accounts import child_key_refusal, role_key
from shelfhold.export.targets.kavita.client import CallResult
from shelfhold.export.targets.kavita.errors import ProtocolError, RouteNotAllowed, TargetError
from shelfhold.export.targets.kavita.routes import Route
from shelfhold.infrastructure.fetcher import FetchError, GuardedFetcher
from shelfhold.infrastructure.kavita_names import same_kavita_username
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.schema import export

__all__ = [
    "CONTINUE_POINT",
    "GET_PROGRESS",
    "KOSYNC_GET",
    "KOSYNC_PUT",
    "MEMBER_ROUTES",
    "POST_PROGRESS",
    "SERIES_PROGRESS",
    "WHOAMI",
    "AccountCheckFailed",
    "DestinationInfo",
    "KavitaMemberVerifier",
    "MemberKavitaClient",
    "MemberKey",
    "Verified",
    "WhoAmI",
    "run_sync",
]

log = structlog.get_logger(__name__)

# `GET /api/Account` answers a `UserDto`: the name, and also a session token and the account's own
# default key. Only the name is read; the rest is dropped with the response and never kept.
WHOAMI: Final = Route("GET", "/api/Account", write=False)
GET_PROGRESS: Final = Route("GET", "/api/Reader/get-progress", write=False)
POST_PROGRESS: Final = Route("POST", "/api/Reader/progress", write=True)
CONTINUE_POINT: Final = Route("GET", "/api/Reader/continue-point", write=False)
# The series list of a library, read with the member's key: `pagesRead` and `latestReadDate` are the
# account's own, a cheap sign of which series moved (the same route the dedicated key reads, a read
# despite the POST).
SERIES_PROGRESS: Final = Route("POST", "/api/Series/all-v2", write=False)

# The kosync routes of Kavita (`KoreaderController`): how an EPUB goes out, by its share alone
# (Kavita
# counts an EPUB in documents, ours is a share). The key is in the PATH (`{apiKey}`); `{ebookHash}`
# is
# the partial md5 of the file, as KOReader computes it. `users/auth` and `users/create` are not
# here.
KOSYNC_GET: Final = Route("GET", "/api/Koreader/{apiKey}/syncs/progress/{ebookHash}", write=False)
KOSYNC_PUT: Final = Route("PUT", "/api/Koreader/{apiKey}/syncs/progress", write=True)

MEMBER_ROUTES: Final = (
    WHOAMI, GET_PROGRESS, POST_PROGRESS, CONTINUE_POINT, SERIES_PROGRESS, KOSYNC_GET, KOSYNC_PUT,
)  # fmt: skip
_BY_KEY: Final[dict[tuple[str, str], Route]] = {
    (r.method, r.path.lower()): r for r in MEMBER_ROUTES
}
_MAX_NAME: Final = 128
_EBOOK_HASH = re.compile(r"[0-9a-f]{32}")
_PLACEHOLDER = re.compile(r"\{([A-Za-z]+)\}")


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
    allowed_profiles: tuple[str, ...] = ()  # the shelf's own list; empty: open to everyone
    shelf_name: str = ""
    target_name: str = ""


@dataclass(frozen=True)
class WhoAmI:
    """What of `GET /api/Account` is read: the name, the roles and the restriction. The session
    token and the account's own key are never read."""

    username: str
    roles: tuple[str, ...]
    age_rating: int | None  # None: the answer carries no restriction
    include_unknowns: bool

    @property
    def is_admin(self) -> bool:
        return any(role_key(r) == "admin" for r in self.roles)


@dataclass(frozen=True)
class Verified:
    is_admin: bool


class MemberKey:
    """The key of one member's Kavita account. It does not print."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("an empty Kavita key")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "MemberKey(<redacted>)"

    __str__ = __repr__


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """`Retry-After` in seconds (the form Kavita and its proxies use), or None."""
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                seconds = float(value)
            except ValueError:
                return None
            return seconds if seconds >= 0 else None
    return None


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
        path_params: Mapping[str, str] | None = None,
    ) -> CallResult:
        route = _resolve(method, path)
        headers = {"Accept": "application/json"}
        if "{apiKey}" in route.path:
            url = self._base_url + self._kosync_path(route, path_params)
        else:
            if path_params:
                raise RouteNotAllowed(method, path)
            headers["x-api-key"] = self._key.reveal()
            url = self._base_url + route.path
        try:
            if route.method == "GET":
                response = await self._fetcher.get(
                    url, headers=headers, params=dict(query or {}) or None
                )
            elif route.method == "PUT":
                response = await self._fetcher.put(
                    url, json=body if body is not None else {}, headers=headers
                )
            else:
                if query:  # the fetcher takes the parameters of a POST in the URL
                    url += "?" + urlencode(dict(query))
                response = await self._fetcher.post(
                    url, json=body if body is not None else {}, headers=headers
                )
        except FetchError as exc:
            log.warning("kavita.member_unreachable", route=route.label, reason=type(exc).__name__)
            raise ProtocolError(f"{route.label}: unreachable") from None
        log.debug("kavita.member_call", route=route.label, status=response.status)
        if response.status != 200:  # a 401 or 403 is Kavita refusing the key
            raise ProtocolError(
                f"{route.label} answered {response.status}", status=response.status,
                retry_after=_retry_after(response.headers),
            )  # fmt: skip
        try:
            data = json.loads(response.body) if response.body else None
        except ValueError:
            raise ProtocolError(f"{route.label}: the answer is not JSON") from None
        return CallResult(response.status, data)

    def _kosync_path(self, route: Route, params: Mapping[str, str] | None) -> str:
        """The path of a kosync route: the key is the client's own and registered to be masked, the
        digest is the one value a caller gives, and it must be a lower-case md5. Any other name, or
        a
        caller that names the key, is a refusal before a request is built."""
        given = dict(params or {})
        wanted = set(_PLACEHOLDER.findall(route.path)) - {"apiKey"}
        if set(given) != wanted:
            raise RouteNotAllowed(route.method, route.path)
        if "ebookHash" in given and _EBOOK_HASH.fullmatch(given["ebookHash"]) is None:
            raise RouteNotAllowed(route.method, route.path)
        register_secret_value(self._key.reveal())
        values = {"apiKey": quote(self._key.reveal(), safe=""), **given}
        return _PLACEHOLDER.sub(lambda m: values[m.group(1)], route.path)

    async def whoami(self) -> WhoAmI:
        """The account the key opens. Nothing else of the answer leaves this method."""
        result = await self.call(WHOAMI.method, WHOAMI.path)
        data = result.data
        if not isinstance(data, dict):
            raise ProtocolError("GET /api/Account: no account in the answer")
        name = data.get("username")
        if not isinstance(name, str) or not name or len(name) > _MAX_NAME:
            raise ProtocolError("GET /api/Account: no account name in the answer")
        roles = data.get("roles")
        restriction = data.get("ageRestriction")
        rating: int | None = None
        unknowns = True
        if isinstance(restriction, dict) and isinstance(restriction.get("ageRating"), int):
            rating = restriction["ageRating"]
            unknowns = bool(restriction.get("includeUnknowns", True))
        return WhoAmI(
            name,
            tuple(str(r) for r in roles) if isinstance(roles, list) else (),
            rating,
            unknowns,
        )


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
        table, target, shelf = export.shelf_destination, export.export_target, export.shelf
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(table.c.id, target.c.kind, shelf.c.allowed_profiles)
                .join(target, target.c.id == table.c.target_id)
                .join(shelf, shelf.c.id == table.c.shelf_id)
                .where(table.c.id == destination_id)
            ).first()
        if row is None:
            return None
        listed = row.allowed_profiles if isinstance(row.allowed_profiles, list) else []
        return DestinationInfo(row.id, row.kind == "kavita", tuple(str(p) for p in listed))

    def kavita_destinations(self) -> list[DestinationInfo]:
        """Every binding to a Kavita, with the name of its shelf and of its server (what a member
        reads to choose the account they link a key to), in a stable order."""
        table, target, shelf = export.shelf_destination, export.export_target, export.shelf
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    table.c.id,
                    shelf.c.name.label("shelf_name"),
                    shelf.c.allowed_profiles,
                    target.c.name.label("target_name"),
                )
                .join(target, target.c.id == table.c.target_id)
                .join(shelf, shelf.c.id == table.c.shelf_id)
                .where(target.c.kind == "kavita")
                .order_by(shelf.c.name, target.c.name, table.c.id)
            ).all()
        return [
            DestinationInfo(
                row.id,
                True,
                tuple(str(p) for p in row.allowed_profiles)
                if isinstance(row.allowed_profiles, list)
                else (),
                row.shelf_name,
                row.target_name,
            )
            for row in rows
        ]

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

    def verify(
        self, destination_id: str, remote_user: str, token: str, *, child_max_age: int | None = None
    ) -> Verified:
        target = self._target(destination_id)
        if target.kind != "kavita" or not target.url or not urlsplit(target.url).hostname:
            raise AccountCheckFailed("destination_not_supported")
        client = MemberKavitaClient(self._fetcher_for(target.url), target.url, MemberKey(token))
        try:
            who = run_sync(client.whoami())
        except ProtocolError as exc:
            if exc.status in (401, 403):
                raise AccountCheckFailed("token_refused") from None
            raise AccountCheckFailed("destination_unreachable") from None
        if not same_kavita_username(who.username, remote_user):
            raise AccountCheckFailed(
                "token_refused"
            )  # a real key, of another account: not said whose
        if child_max_age is not None:
            refusal = child_key_refusal(
                who.roles, who.age_rating, who.include_unknowns, child_max_age
            )
            if refusal is not None:  # a child's key opens an account that is a child's
                raise AccountCheckFailed(refusal)
        return Verified(who.is_admin)


def run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to its end from synchronous code, in a thread when a loop is already
    running in this one (a request handler runs in the pool of the server, not in a loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: list[T] = []
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
