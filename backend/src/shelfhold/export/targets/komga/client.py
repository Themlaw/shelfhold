"""The Komga client of the core: allow-listed routes, a debounced scan of the library.

It runs the `ApiCall`s a destination describes and holds the key; the key goes in the `X-API-Key`
header at the last moment and appears nowhere else: not in a URL, a body, an error, a repr or a
log line. All traffic goes through the `IntegrationFetcher` of the target (declared LAN target,
SSRF guard).

A scan is the scan of a **library**. `request_scan_for_path` maps a folder to the library whose
root holds it (by components, so `/manga-extra` is not under `/manga`) and the debouncer keeps
the library: thirty folders of one library are one scan, sent once the writing has been quiet.
After a deletion the library is scanned, then its trash is emptied: Komga forgets the books the
host removed, and the records of no other library.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from urllib.parse import urlencode

import structlog

from shelfhold.export.domain.komga_access import KomgaAccount, restriction_age
from shelfhold.export.domain.scan_debounce import DEFAULT_QUIET, ScanDebouncer
from shelfhold.export.targets.kavita.errors import (
    ProtocolError,
    TargetForbidden,
    TargetKeyRevoked,
    TargetUnreachable,
)
from shelfhold.export.targets.komga import routes
from shelfhold.export.targets.komga.credentials import KomgaKey
from shelfhold.export.targets.komga.errors import KomgaNoLibrary
from shelfhold.export.targets.komga.target import KomgaDestination
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fetcher import FetchError, GuardedFetcher
from shelfhold.infrastructure.logging import elapsed_ms
from shelfhold.sdk.api_call import ApiCall

MAX_LIBRARIES: Final = 500
MAX_ACCOUNTS: Final = 5000

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CallResult:
    status: int
    data: Any  # decoded JSON, None when the answer has no body


@dataclass(frozen=True)
class Library:
    id: str
    name: str
    root: str


def _account(row: Any) -> KomgaAccount:
    """One account of `GET /api/v2/users`. Strict: a field of the wrong type is an error, never a
    default (a default could read as "no restriction")."""
    if not isinstance(row, dict):
        raise TypeError("not an account")
    roles, shared, ids = row["roles"], row["sharedAllLibraries"], row["sharedLibrariesIds"]
    restriction = row.get("ageRestriction")
    if not (isinstance(roles, list) and all(isinstance(r, str) for r in roles)):
        raise TypeError("roles")
    if not isinstance(shared, bool) or not (
        isinstance(ids, list) and all(isinstance(i, str) for i in ids)
    ):
        raise TypeError("shares")
    age = None
    if restriction is not None:
        years = restriction["age"]
        if (
            not isinstance(restriction, dict)
            or isinstance(years, bool)
            or not isinstance(years, int)
        ):
            raise TypeError("age restriction")
        if years < 0:
            raise ValueError("age restriction")
        age = restriction_age(years, str(restriction["restriction"]))
    return KomgaAccount(
        str(row["email"]), "ADMIN" in roles, shared, frozenset(ids), age
    )  # the name tells accounts apart during the audit only; it is stored nowhere


def _under(root: str, path: str) -> bool:
    """`path` is `root` or inside it, compared by components."""
    base = [p for p in root.split("/") if p]
    parts = [p for p in path.split("/") if p]
    return len(parts) >= len(base) and parts[: len(base)] == base


class KomgaClient:
    def __init__(
        self,
        fetcher: GuardedFetcher,
        base_url: str,
        key: KomgaKey,
        *,
        clock: Clock,
        scan_quiet: timedelta = DEFAULT_QUIET,
    ) -> None:
        self._fetcher = fetcher
        self._base_url = base_url.rstrip("/")
        self._key = key
        self._scans = ScanDebouncer(clock, scan_quiet)  # holds library ids, not folders
        self._deleted: set[str] = set()
        self._destination = KomgaDestination()
        self._write_guard: Callable[[], None] | None = None
        self._libraries: list[Library] | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    def __repr__(self) -> str:
        return f"KomgaClient({self._base_url!r})"

    def set_write_guard(self, guard: Callable[[], None] | None) -> None:
        """Called before every write route: raises while the destination is paused."""
        self._write_guard = guard

    # -- the one way out ------------------------------------------------------------------------

    async def execute(self, call: ApiCall) -> CallResult:
        """Run a call a destination described, if and only if its route is on the list."""
        route, _library = routes.resolve(call.method, call.path.split("?", 1)[0])
        if call.body is not None or call.json_body is not None:
            raise ProtocolError("Komga takes no body on the routes of the list")
        if set(call.query) - (routes.SCAN_QUERY if route is routes.LIBRARY_SCAN else frozenset()):
            raise ProtocolError("a parameter the route does not declare")
        return await self._send(
            route, call.path.split("?", 1)[0], query=call.query, expect=call.expect
        )

    async def _send(
        self,
        route: routes.Route,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        expect: list[int] | None = None,
    ) -> CallResult:
        if route.write and self._write_guard is not None:
            self._write_guard()
        headers = {"X-API-Key": self._key.reveal(), "Accept": "application/json"}
        url = self._base_url + path
        started = time.perf_counter()
        try:
            if route.method == "GET":
                response = await self._fetcher.get(
                    url, headers=headers, params=dict(query or {}) or None
                )
            else:
                if query:  # the fetcher takes the parameters of a POST in the URL
                    url += "?" + urlencode(dict(query))
                response = await self._fetcher.post(url, headers=headers)
        except FetchError as error:
            log.warning("komga.unreachable", route=route.label, reason=type(error).__name__,
                        duration_ms=elapsed_ms(started))  # fmt: skip
            raise TargetUnreachable(f"{route.label}: {type(error).__name__}") from None
        # the route label only: never the URL, which names a library and may carry a parameter
        log.debug("komga.call", route=route.label, status=response.status,
                  duration_ms=elapsed_ms(started))  # fmt: skip
        if response.status == 401:
            log.error("komga.key_revoked", route=route.label)
            raise TargetKeyRevoked(route.label)
        if response.status == 403:
            log.error("komga.forbidden", route=route.label)
            raise TargetForbidden(route.label)
        if response.status == 404:
            raise ProtocolError(f"{route.label}: resource not found", status=404)
        if response.status not in (expect or [200]):
            raise ProtocolError(f"{route.label} answered {response.status}", status=response.status)
        return CallResult(response.status, _decode(response.body, route))

    # -- connection -----------------------------------------------------------------------------

    async def probe(self) -> str | None:
        """The version Komga says it is (`/actuator/info`, an administrator's route)."""
        result = await self._send(routes.INFO, routes.INFO.path)
        build = result.data.get("build") if isinstance(result.data, dict) else None
        version = build.get("version") if isinstance(build, dict) else None
        return version if isinstance(version, str) else None

    async def list_accounts(self) -> list[KomgaAccount]:
        """The accounts of the server (`GET /api/v2/users`, an administrator's route). Any row that
        cannot be read as an account fails the whole reading: the audit never skips an account."""
        result = await self._send(routes.USERS, routes.USERS.path)
        if not isinstance(result.data, list) or len(result.data) > MAX_ACCOUNTS:
            raise ProtocolError("GET /api/v2/users did not answer a list of accounts")
        try:
            return [_account(row) for row in result.data]
        except (KeyError, TypeError, ValueError):
            raise ProtocolError("GET /api/v2/users: an account cannot be read") from None

    async def list_libraries(self) -> list[Library]:
        result = await self._send(routes.LIBRARIES, routes.LIBRARIES.path)
        rows = result.data if isinstance(result.data, list) else []
        libraries = [
            Library(str(r["id"]), str(r.get("name", "")), str(r["root"]))
            for r in rows[:MAX_LIBRARIES]
            if isinstance(r, dict)
            and isinstance(r.get("id"), str)
            and isinstance(r.get("root"), str)
        ]
        self._libraries = libraries
        return libraries

    async def library_of(self, remote_path: str) -> str:
        """The library whose root holds this folder (the deepest root when two nest)."""
        libraries = self._libraries if self._libraries is not None else await self.list_libraries()
        holding = [lib for lib in libraries if _under(lib.root, remote_path)]
        if not holding:
            raise KomgaNoLibrary("no library of the server holds this folder")
        return max(holding, key=lambda lib: len([p for p in lib.root.split("/") if p])).id

    # -- the scan of a library --------------------------------------------------------------------

    async def request_scan_for_path(self, remote_path: str) -> None:
        """Ask for the scan of the library that holds this folder; nothing is sent yet."""
        self._scans.request(await self.library_of(remote_path))

    def request_scan(self, library_id: str) -> None:
        self._scans.request(library_id)

    def note_deletion(self, library_id: str) -> None:
        """A file of this library was removed by the host: its trash is emptied after its scan."""
        self._deleted.add(library_id)
        self._scans.request(library_id)

    async def flush_scans(self, *, now: bool = False) -> list[str]:
        """Send the scans due (`now`: all that were asked), each library once. Every scan is
        followed by the emptying of the trash of a library that lost a book."""
        due = self._scans.take_all() if now else self._scans.take_due()
        sent: list[str] = []
        for library_id in due:
            await self.execute(self._destination.scan_library_call(library_id))
            sent.append(library_id)
            if library_id in self._deleted:
                await self.execute(self._destination.forget_removed_call(library_id))
                self._deleted.discard(library_id)
        return sent


def _decode(raw: bytes, route: routes.Route) -> Any:
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise ProtocolError(f"{route.label} did not answer JSON") from None
