"""The Kavita client of the core: allow-listed routes, version range, degraded modes, 403.

It runs the `ApiCall`s a destination describes and holds the admin key; the key is put in the
`x-api-key` header (and in the body of `scan-folder`, whose DTO has an `apiKey`) at the last
moment and appears nowhere else: not in a URL, an event, an error, a repr or the health document.
All traffic goes through the `IntegrationFetcher` of the target (declared LAN target, SSRF guard).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlencode

import structlog

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.target_health import TargetHealth
from shelfhold.export.domain.kavita_compat import (
    KNOWN_BAD_VERSIONS,
    MINIMUM_VERSION,
    Assessment,
    KnownBad,
    TargetMode,
    assess_version,
    parser_replica_covers,
)
from shelfhold.export.domain.kavita_key_expiry import KeyNotice, key_notices
from shelfhold.export.domain.scan_debounce import DEFAULT_QUIET, ScanDebouncer
from shelfhold.export.targets.kavita import dto, routes
from shelfhold.export.targets.kavita.credentials import AdminKey
from shelfhold.export.targets.kavita.destination import KavitaDestination
from shelfhold.export.targets.kavita.errors import (
    ConfirmationRequired,
    ListingIncomplete,
    ProtocolError,
    RouteMissing,
    TargetAsleep,
    TargetDegraded,
    TargetForbidden,
    TargetKeyRevoked,
    TargetUnreachable,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fetcher import FetchError, GuardedFetcher
from shelfhold.infrastructure.logging import elapsed_ms
from shelfhold.infrastructure.wake import WakeDeferred
from shelfhold.sdk.export_target import ApiCall

BULK_BATCH: Final = 100  # `parse-bulk` takes 100 names at most
SERIES_PAGE: Final = 100
MAX_SERIES_PAGES: Final = 50


log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CallResult:
    status: int
    data: Any  # decoded JSON, None when the answer has no body


class KavitaClient:
    def __init__(
        self,
        fetcher: GuardedFetcher,
        base_url: str,
        key: AdminKey,
        *,
        events: TargetEvents,
        clock: Clock,
        health: TargetHealth | None = None,
        scan_quiet: timedelta = DEFAULT_QUIET,
        known_bad: tuple[KnownBad, ...] | None = None,
        wake: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._known_bad = KNOWN_BAD_VERSIONS if known_bad is None else known_bad
        self._fetcher = fetcher
        self._base_url = base_url.rstrip("/")
        self._key = key
        self._events = events
        self._clock = clock
        self.health = health or TargetHealth()
        self._scans = ScanDebouncer(clock, scan_quiet)
        self._destination = KavitaDestination()
        self._unreachable_announced = False
        self._key_revoked = False  # the pause is the revocation of the key (401)
        self._write_guard: Callable[[], None] | None = None
        self._wake = wake  # EF-INT-022: a Kavita behind Sablier is woken before it is called

    def set_write_guard(self, guard: Callable[[], None] | None) -> None:
        """Called before every write route: raises while the destination is paused."""
        self._write_guard = guard

    @contextmanager
    def unguarded(self) -> Iterator[None]:
        """`target reconnect` writes (library settings) while the exports are paused."""
        guard, self._write_guard = self._write_guard, None
        try:
            yield
        finally:
            self._write_guard = guard

    def __repr__(self) -> str:
        return f"KavitaClient({self._base_url!r}, mode={self.health.mode.value})"

    # -- state ---------------------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def collision_check_available(self) -> bool:
        return not routes.PARSE_ROUTES & set(self.health.missing_routes)

    @property
    def preview_verified(self) -> bool:
        """False when the name check is unavailable, or when the version probed is not one the
        replica of the parser covers: the preview is shown as "not verified" (EF-EXP-005)."""
        return self.collision_check_available and parser_replica_covers(self.health.version)

    # -- the one way out -----------------------------------------------------------------------

    async def execute(self, call: ApiCall) -> CallResult:
        """Run a call described by a destination, if and only if its route is on the list."""
        route = routes.resolve(call.method, call.path)
        body = call.json_body
        if route is routes.SCAN_FOLDER:
            if body is not None and not isinstance(body, dict):
                raise ProtocolError("scan-folder takes a JSON object")
            body = {**(body or {}), "apiKey": self._key.reveal()}  # the DTO of Kavita asks for it
        return await self._send(route, query=call.query, body=body, expect=call.expect)

    async def _send(
        self,
        route: routes.Route,
        *,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
        expect: Sequence[int] = (200,),
    ) -> CallResult:
        health = self.health
        if health.paused:
            raise (TargetKeyRevoked if self._key_revoked else TargetForbidden)(route.label)
        await self._awake(route)
        if route.label in health.missing_routes:
            raise RouteMissing(route.label)
        if route is not routes.SERVER_INFO and health.version is None:
            await self.probe()  # a version is read before the first call that needs a verdict
        if route.write and self._write_guard is not None:
            self._write_guard()
        if route.write and health.version_mode is TargetMode.DEGRADED_READONLY:
            raise TargetDegraded(f"{route.label} is a write: the Kavita version is out of range")
        headers = {"x-api-key": self._key.reveal(), "Accept": "application/json"}
        url = self._base_url + route.path
        if route.method != "GET" and query:  # the fetcher takes the parameters of a POST in the URL
            url += "?" + urlencode(dict(query))
        started = time.perf_counter()
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
            log.warning("kavita.unreachable", route=route.label, method=route.method,
                        reason=type(exc).__name__, duration_ms=elapsed_ms(started))  # fmt: skip
            if not self._unreachable_announced:
                self._unreachable_announced = True
                self._events.emit("target.unreachable", route=route.label)
            raise TargetUnreachable(f"{route.label}: {type(exc).__name__}") from None
        self._unreachable_announced = False
        # the route label only: some Kavita routes carry the key in their query string
        log.debug("kavita.call", route=route.label, method=route.method, status=response.status,
                  duration_ms=elapsed_ms(started))  # fmt: skip
        if response.status == 404:
            if _is_json(
                response.body
            ):  # a ProblemDetails or JSON body: the resource, not the route
                raise ProtocolError(f"{route.label}: resource not found")
            self._route_missing(route)
        if response.status == 401:
            self._revoked(route)
        if response.status == 403:
            self._forbidden(route)
        if response.status not in expect:
            raise ProtocolError(f"{route.label} answered {response.status}")
        return CallResult(response.status, _decode(response.body, route))

    async def _awake(self, route: routes.Route) -> None:
        """Before any call (`scan-folder`, the audit, the arrival check): wake Kavita. A Kavita
        that stays asleep puts the batch off. It is not `target.unreachable`, and since every
        write starts from a call, nothing is written."""
        if self._wake is None:
            return
        try:
            await self._wake()
        except WakeDeferred:
            log.info("kavita.asleep", route=route.label)
            raise TargetAsleep(
                f"{route.label}: Kavita is asleep and was not woken in time"
            ) from None

    def _route_missing(self, route: routes.Route) -> None:
        health = self.health
        log.warning("kavita.route_missing", route=route.label)
        health.missing_routes = (*health.missing_routes, route.label)
        health.refresh_waiting()
        self._events.emit("target.route_missing", route=route.label)
        raise RouteMissing(route.label)

    def _revoked(self, route: routes.Route) -> None:
        """401: the key was revoked (matrix row `kavita_key_revoked`). The destination is disabled
        at once, with the same exit as a lost right (`target reconnect`), but announced as the
        server no longer answering for us: `target.unreachable`."""
        health = self.health
        log.error("kavita.key_revoked", route=route.label)
        health.paused, health.paused_route = True, route.label
        self._key_revoked = True
        health.refresh_waiting()
        self._events.emit("target.unreachable", route=route.label)
        raise TargetKeyRevoked(route.label)

    def _forbidden(self, route: routes.Route) -> None:
        health = self.health
        log.error("kavita.forbidden", route=route.label)
        health.paused, health.paused_route = True, route.label
        health.refresh_waiting()
        self._events.emit("target.forbidden", route=route.label)
        raise TargetForbidden(route.label)

    # -- connection ----------------------------------------------------------------------------

    async def probe(self) -> Assessment:
        """Read the version and decide the mode (ENF-COMP-001); known-bad versions only warn."""
        result = await self._send(routes.SERVER_INFO)
        raw = result.data.get("kavitaVersion") if isinstance(result.data, dict) else None
        assessment = assess_version(
            raw if isinstance(raw, str) else None, known_bad=self._known_bad
        )
        health = self.health
        log.debug("kavita.probed", version=assessment.raw, mode=assessment.mode.value)
        health.version, health.version_mode = assessment.raw or None, assessment.mode
        install_id = result.data.get("installId") if isinstance(result.data, dict) else None
        health.install_id = install_id if isinstance(install_id, str) else None
        health.warnings = tuple(f"{k.advisory} ({k.category})" for k in assessment.known_bad)
        if assessment.mode is TargetMode.DEGRADED_READONLY:
            if health.version_event_for != assessment.raw:
                health.version_event_for = assessment.raw
                self._events.emit(
                    "target.version_unsupported",
                    version=assessment.raw,
                    minimum=str(MINIMUM_VERSION),
                )
        else:
            health.version_event_for = None
        return assessment

    async def recheck(self) -> Assessment:
        """`target recheck`: forget the missing routes and look again (after a Kavita update)."""
        self.health.missing_routes = ()
        self.health.refresh_waiting()
        return await self.probe()

    async def reconnect(self) -> Assessment:
        """`target reconnect`: leave the pause of a 403 once the account has its rights back."""
        health = self.health
        health.paused, health.paused_route = False, None
        self._key_revoked = False
        health.refresh_waiting()
        return await self.probe()

    async def check_key_expiry(self) -> list[KeyNotice]:
        """Read the key date again (it can change in Kavita); send the due J-14/J-1 notices."""
        result = await self._send(routes.KEY_EXPIRES)
        raw = result.data.get("expiresAt") if isinstance(result.data, dict) else None
        expires = _moment(raw)
        health = self.health
        notices, health.key_notices = key_notices(expires, self._clock.now(), health.key_notices)
        health.key_expires_at = expires
        for notice in notices:
            self._events.emit(
                "target.key_expiring",
                threshold=notice.threshold,
                days_left=notice.days_left,
                expires_at=expires.isoformat() if expires else None,
            )
        health.refresh_waiting()
        return notices

    # -- reads ---------------------------------------------------------------------------------

    async def parse_name(self, name: str, library_type: dto.LibraryType) -> dto.ParseResult:
        call = self._destination.parse_name_call(name, library_type.kavita_name)
        if call is None:
            raise ProtocolError("no parse call for this library type")
        return dto.ParseResult.from_json((await self.execute(call)).data)

    async def parse_bulk(
        self, names: Sequence[str], library_type: dto.LibraryType
    ) -> dict[str, dto.ParseResult]:
        found: dict[str, dto.ParseResult] = {}
        for start in range(0, len(names), BULK_BATCH):
            body = {
                "names": list(names[start : start + BULK_BATCH]),
                "libraryType": int(library_type),
            }
            result = await self._send(routes.PARSE_BULK, body=body)
            results = result.data.get("results") if isinstance(result.data, dict) else None
            if not isinstance(results, dict):
                raise ProtocolError("parse-bulk did not answer results")
            found.update({name: dto.ParseResult.from_json(item) for name, item in results.items()})
        return found

    async def list_libraries(self) -> list[dict[str, Any]]:
        data = (await self._send(routes.LIBRARIES)).data
        if not isinstance(data, list):
            raise ProtocolError("libraries did not answer a list")
        return [item for item in data if isinstance(item, dict)]

    async def library_type(self, library_id: int) -> dto.LibraryType:
        library_id = int(library_id)
        data = (await self._send(routes.LIBRARY_TYPE, query={"libraryId": library_id})).data
        try:
            return dto.LibraryType(int(data))
        except (TypeError, ValueError):
            raise ProtocolError("library type is not a known value") from None

    async def list_users(self) -> list[dto.Member]:
        """The accounts, read-only; an administrator is needed (a 403 pauses the destination)."""
        data = (await self._send(routes.USERS, query={"includePending": "true"})).data
        if not isinstance(data, list):
            raise ProtocolError("users did not answer a list")
        return [dto.Member.from_json(item) for item in data]

    async def series_of_library(
        self, library_id: int, *, max_pages: int | None = None
    ) -> list[dto.SeriesInfo]:
        """The series of a library, capped (`max_pages` of 100): never an unbounded read, and never
        a silently cut one (`ListingIncomplete` at the cap)."""
        max_pages = MAX_SERIES_PAGES if max_pages is None else max_pages
        found: list[dto.SeriesInfo] = []
        body = dto.library_filter(int(library_id))
        for page in range(1, max_pages + 1):
            query = {"PageNumber": page, "PageSize": SERIES_PAGE}
            data = (await self._send(routes.SERIES_OF_LIBRARY, query=query, body=body)).data
            if not isinstance(data, list):
                raise ProtocolError("all-v2 did not answer a list")
            found.extend(dto.SeriesInfo.from_json(item) for item in data)
            if len(data) < SERIES_PAGE:
                return found
        raise ListingIncomplete(f"the library has at least {max_pages * SERIES_PAGE} series")

    async def series_by_ids(self, series_ids: Sequence[int]) -> dict[int, dto.SeriesInfo]:
        """The current state (name included) of series known by their cached id."""
        if not series_ids:
            return {}
        body = {"seriesIds": [int(i) for i in series_ids]}
        data = (await self._send(routes.SERIES_BY_IDS, body=body)).data
        if not isinstance(data, list):
            raise ProtocolError("series-by-ids did not answer a list")
        series = [dto.SeriesInfo.from_json(item) for item in data]
        return {item.id: item for item in series}

    async def series_files(self, series_id: int) -> list[str]:
        """The files Kavita has indexed for a series (volumes, chapters and specials)."""
        return [f.path for f in await self.series_chapters(series_id)]

    async def series_chapters(self, series_id: int) -> list[dto.ChapterFile]:
        """The same files with the progress of the account of the key, as Kavita's API gives it
        (`pagesRead` of each chapter): what the control before an unmount compares."""
        data = (await self._send(routes.SERIES_DETAIL, query={"seriesId": int(series_id)})).data
        if not isinstance(data, dict):
            raise ProtocolError("series-detail did not answer an object")
        chapters = list(data.get("chapters") or []) + list(data.get("specials") or [])
        for volume in data.get("volumes") or []:
            chapters.extend(volume.get("chapters") or [] if isinstance(volume, dict) else [])
        return [
            dto.ChapterFile(str(f["filePath"]), _count(chapter.get("pages")),
                            _count(chapter.get("pagesRead")))
            for chapter in chapters
            if isinstance(chapter, dict)
            for f in chapter.get("files") or []
            if isinstance(f, dict) and "filePath" in f
        ]  # fmt: skip

    async def series_age(self, series_id: int) -> dto.SeriesAge:
        data = (await self._send(routes.SERIES_METADATA, query={"seriesId": int(series_id)})).data
        return dto.SeriesAge.from_json(data)

    # -- writes --------------------------------------------------------------------------------

    async def set_series_age(self, current: dto.SeriesAge, age_rating: int) -> None:
        """Push the age of a series, locked, as a faithful update of the document just read."""
        await self._send(routes.SERIES_METADATA_UPDATE, body=current.age_update(age_rating))

    async def set_series_field(self, current: dto.SeriesAge, key: str, value: str) -> None:
        """Set one metadata field of a series, locked, as a faithful update of the document read."""
        body = dto.field_update(current.document, current.series_id, key, value)
        await self._send(routes.SERIES_METADATA_UPDATE, body=body)

    async def create_library(
        self,
        name: str,
        library_type: dto.LibraryType,
        folders: Sequence[str],
        exclude_patterns: Sequence[str] = dto.HIDDEN_DIR_PATTERNS,
    ) -> dict[str, Any]:
        body = dto.create_dto(name, library_type, folders, exclude_patterns)
        data = (await self._send(routes.LIBRARY_CREATE, body=body)).data
        if not isinstance(data, dict):
            raise ProtocolError("create did not answer the library")
        return data

    async def ensure_exclude_patterns(
        self,
        library_id: int,
        wanted: Sequence[str],
        *,
        adopted: bool = False,
        confirmed: bool = False,
    ) -> dto.LibraryUpdate:
        """GET then faithful update: the library as read, with the missing patterns added.

        An adopted library is never touched without the admin's confirmation (EF-EXP-009); the
        patterns it had are returned so that detaching can put them back.
        """
        library_id = int(library_id)
        current = next(
            (lib for lib in await self.list_libraries() if lib.get("id") == library_id), None
        )
        if current is None:
            raise ProtocolError(f"library {library_id} is not in Kavita")
        before = tuple(current.get("excludePatterns") or ())
        patterns = dto.merged_patterns(before, wanted)
        if tuple(patterns) == before:
            return dto.LibraryUpdate(library_id, False, before)
        if adopted and not confirmed:
            raise ConfirmationRequired(f"library {library_id} is adopted: confirm the change")
        body = dto.update_dto(current, exclude_patterns=patterns)
        await self._send(routes.LIBRARY_UPDATE, body=body)
        return dto.LibraryUpdate(library_id, True, before)

    async def scan_folder(self, remote_path: str) -> None:
        for call in self._destination.scan_calls([remote_path]):
            await self.execute(call)

    def request_scan(self, remote_path: str) -> None:
        """Ask for a scan; `flush_scans` sends it once the writing has been quiet."""
        self._scans.request(remote_path)

    async def flush_scans(self, allowed: Callable[[str], bool] | None = None) -> list[str]:
        """Send the scans due. `allowed(remote_path)` is the sentinel's word on the folder
        (`require_scan_allowed`): a scan it refuses is not sent, and the next run asks again."""
        due = self._scans.take_due()
        sent = []
        for path in due:
            if allowed is not None and not allowed(path):
                continue
            await self.scan_folder(path)
            sent.append(path)
        return sent

    async def upload_series_cover(self, series_id: int, image: bytes) -> None:
        """Replace and lock the cover: bare base64 (no `data:` prefix), as the DTO wants."""
        body = {
            "id": int(series_id),
            "url": base64.b64encode(image).decode("ascii"),
            "lockCover": True,
        }
        await self._send(routes.UPLOAD_SERIES_COVER, body=body)


def _count(value: object) -> int:
    """A page count from Kavita: an integer, else 0 (never trusted further than that)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _is_json(raw: bytes) -> bool:
    try:
        json.loads(raw)
    except (ValueError, RecursionError):
        return False
    return True


def _decode(raw: bytes, route: routes.Route) -> Any:
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise ProtocolError(f"{route.label} did not answer JSON") from None


def _moment(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
