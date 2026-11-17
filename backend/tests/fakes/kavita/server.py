"""A simulated Kavita server, in process, synthetic only (ENF-COMP-001, ENF-LEG-013).

It answers the routes of `docs/kavita-routes.md` the way the `openapi.json` of Kavita 0.9.1.x
describes them (named auth key in `x-api-key`, `scan-folder` reading `apiKey` in its body, covers
as bare base64) and plays the host's referee: any request outside its own allow-list, any key in
a URL, any body field the DTO does not declare, is answered 400 and recorded in `violations`.
The allow-list is written here on purpose, independently of the adapter under test. The DTO
shapes are read from the reduced `openapi.json` fixture of the pinned version.

The simulated server is wired to the real `IntegrationFetcher` (declared LAN target, scripted
DNS, mock transport), so the SSRF guard and the header rules are exercised as in production.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2

from shelfhold.infrastructure.fetcher import IntegrationFetcher, LanTargets
from tests.fakes.kavita.collections import KavitaCollections
from tests.fakes.kavita.plugin_parse import simulate_parse
from tests.fakes.kavita.providers import PROVIDERS_FOR_LIBRARY_TYPE
from tests.fakes_fetcher import FakeResolver

TESTS = Path(__file__).resolve().parents[2]
OPENAPI = TESTS / "fixtures" / "kavita" / "openapi"
BASE_URL = "http://kavita.lan:5000"
LAN_ADDRESS = "192.168.1.50"
API_KEY = "synthetic-admin-key-0001"
OTHER_KEY = "synthetic-admin-key-0002"  # another key of the same account

# (method, lower-cased path) -> needs an administrator. Written here, not imported.
ALLOWED: dict[tuple[str, str], bool] = {
    ("POST", "/api/account/login"): False,
    ("GET", "/api/server/server-info-slim"): False,
    ("GET", "/api/plugin/authkey-expires"): False,
    ("GET", "/api/plugin/parse"): False,
    ("POST", "/api/plugin/parse-bulk"): False,
    ("GET", "/api/library/libraries"): False,
    ("GET", "/api/library/type"): False,
    ("POST", "/api/library/create"): True,
    ("POST", "/api/library/update"): True,
    ("POST", "/api/library/scan-folder"): True,
    ("POST", "/api/upload/series"): True,
    ("GET", "/api/users"): True,
    ("POST", "/api/series/all-v2"): False,
    ("POST", "/api/series/series-by-ids"): False,
    ("GET", "/api/series/series-detail"): False,
    ("GET", "/api/series/metadata"): False,
    ("POST", "/api/series/metadata"): True,
    ("GET", "/api/series/all-related"): False,
    ("POST", "/api/series/update-related"): True,
    # collections and reading lists, under the account of the key (L18b, EF-EXP-025)
    ("GET", "/api/collection"): False,
    ("POST", "/api/collection/update-for-series"): False,
    ("POST", "/api/collection/update-series"): False,
    ("POST", "/api/collection/promote-multiple"): False,
    ("DELETE", "/api/collection"): False,
    ("POST", "/api/readinglist/lists"): False,
    ("GET", "/api/readinglist/items"): False,
    ("POST", "/api/readinglist/create"): False,
    ("POST", "/api/readinglist/update-by-series"): False,
    ("POST", "/api/readinglist/delete-item"): False,
    ("POST", "/api/readinglist/promote-multiple"): False,
    ("DELETE", "/api/readinglist"): False,
    # the routes a member's own key may reach (EF-USR-012, D8): who the key is, and progress
    ("GET", "/api/account"): False,
    ("GET", "/api/reader/get-progress"): False,
    ("POST", "/api/reader/progress"): False,
    ("GET", "/api/reader/continue-point"): False,
}
LIBRARY_TYPES = {"Manga": 0, "Comic": 1, "Book": 2, "Image": 3, "LightNovel": 4, "ComicVine": 5}


def _schema(name: str) -> dict[str, Any]:
    document = json.loads((OPENAPI / "pinned.json").read_text("utf-8"))
    schema: dict[str, Any] = document["components"]["schemas"][name]
    return schema


def default_libraries() -> list[dict[str, Any]]:
    """Two libraries with settings that are not Kavita's defaults, to catch an unfaithful PUT."""
    return [
        {
            "id": 7, "name": "Synthetic Manga", "type": 0, "folders": ["/manga"],
            "folderWatching": False, "includeInDashboard": False, "includeInRecommended": True,
            "manageCollections": False, "manageReadingLists": True, "includeInSearch": False,
            "allowScrobbling": False, "collapseSeriesRelationships": False,
            "libraryFileTypes": [1, 2], "excludePatterns": ["*.tmp"], "allowMetadataMatching": True,
            "enableMetadata": False, "removePrefixForSortName": True,
            "inheritWebLinksFromFirstChapter": True, "defaultLanguage": "ja", "metadataProvider": 3,
        },
        {
            "id": 8, "name": "Synthetic Comics", "type": 1, "folders": ["/comics"],
            "folderWatching": True, "includeInDashboard": True, "includeInRecommended": True,
            "manageCollections": True, "manageReadingLists": True, "includeInSearch": True,
            "allowScrobbling": True, "collapseSeriesRelationships": False,
            "libraryFileTypes": [1], "excludePatterns": [], "allowMetadataMatching": True,
            "enableMetadata": True, "removePrefixForSortName": False,
            "inheritWebLinksFromFirstChapter": False, "defaultLanguage": "", "metadataProvider": 2,
        },
    ]  # fmt: skip


def default_users(created: str = "2026-01-01T00:00:00Z") -> list[dict[str, Any]]:
    return [{**member("admin", admin=True), "createdUtc": created}]


def member(
    username: str,
    *,
    admin: bool = False,
    libraries: tuple[int, ...] = (),
    age_rating: int = -1,
    include_unknowns: bool = True,
) -> dict[str, Any]:
    """A `MemberDto`: -1 is `AgeRating.NotApplicable`, Kavita's "no restriction"."""
    return {
        "id": abs(hash(username)) % 10_000, "username": username, "email": "", "isPending": False,
        "ageRestriction": {"ageRating": age_rating, "includeUnknowns": include_unknowns},
        "libraries": [{"id": i, "name": f"lib {i}"} for i in libraries],
        "roles": ["Admin"] if admin else ["Login"],
    }  # fmt: skip


@dataclass
class PendingIndex:
    """A series that appears once Kavita has scanned its folder `scans_needed` times."""

    series: dict[str, Any]
    files: list[str]
    scans_needed: int = 1
    scans: int = 0


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes = b""
    raw_url: str = ""

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class FakeKavita:
    version: str = "0.9.1.4"
    install_id: str = "synthetic-install-0001"
    key_expires_at: datetime | None = None
    is_admin: bool = True
    libraries: list[dict[str, Any]] = field(default_factory=default_libraries)
    missing: set[tuple[str, str]] = field(default_factory=set)  # (method, path) answered 404
    problem_404: set[tuple[str, str]] = field(default_factory=set)  # 404 with a JSON body
    requests: list[RecordedRequest] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    scans: list[str] = field(default_factory=list)
    ignored_scans: list[str] = field(default_factory=list)
    covers: dict[int, bytes] = field(default_factory=dict)
    parse_calls: list[dict[str, Any]] = field(default_factory=list)
    users: list[dict[str, Any]] = field(default_factory=default_users)
    series: list[dict[str, Any]] = field(default_factory=list)
    files: dict[int, list[str]] = field(default_factory=dict)  # series id -> indexed file paths
    ages: dict[int, dict[str, Any]] = field(default_factory=dict)  # series id -> metadata
    # series id -> relation kind (the DTO's field name) -> the series ids it holds (L19)
    relations: dict[int, dict[str, list[int]]] = field(default_factory=dict)
    # file path -> pages the account of the key has read (`ChapterDto.pagesRead`, EF-EXP-051)
    progress: dict[str, int] = field(default_factory=dict)
    related_posts: list[dict[str, Any]] = field(default_factory=list)  # the bodies taken (L19)
    shelves: KavitaCollections = field(default_factory=KavitaCollections)  # L18b
    pending: list[PendingIndex] = field(default_factory=list)
    never_indexed: set[str] = field(default_factory=set)  # folders Kavita ignores (excluded)
    next_series_id: int = 100
    reinstalls: int = 0
    asleep: bool = False  # stopped by Sablier: the connection is refused until `wake_up`
    drops_age_lock: bool = False  # takes a metadata update but forgets the lock of the age
    manga_parse_only: bool = True  # the replica of plugin/parse is the Manga one: refuse others
    # the keys of the account, by identifier: a request is served only for a key still listed, and
    # a revoked key answers 401 (the detachment of L30 revokes the dedicated one)
    auth_keys: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"id": 1, "name": "shelfhold", "key": API_KEY},
            {"id": 2, "name": "other-tool", "key": OTHER_KEY},
        ]
    )

    account: tuple[str, str] = ("admin", "the account password")  # what the login accepts
    # key -> the user name of the account it opens (`GET /api/Account`); the dedicated keys are
    # the administrator's. Members' keys are added by `add_member_key`.
    key_accounts: dict[str, str] = field(
        default_factory=lambda: {API_KEY: "admin", OTHER_KEY: "admin"}
    )
    member_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Reading progress per ACCOUNT (not per key), as Kavita keeps it (`AppUserProgress`):
    # (user name, chapter id) -> {"page": int, "modified": ISO UTC string}. `clock_skew` is how far
    # this server's own clock is from the real one (a Kavita whose host has a wrong clock).
    member_progress: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    clock_skew: timedelta = timedelta(0)
    chapter_pages: dict[str, int] = field(default_factory=dict)  # file path -> page count
    wall: Any = None  # a `now()` provider (the tests' manual clock); real time when None
    progress_failures: list[tuple[int, dict[str, str]]] = field(default_factory=list)
    # (account, chapter, page) of every write that changed something
    progress_writes: list[tuple[str, int, int]] = field(default_factory=list)
    hook: Any = None  # called with each recorded request, before it is answered (a race)
    inaccessible: set[tuple[str, int]] = field(default_factory=set)  # (account, chapter id): 403
    sessions: set[str] = field(default_factory=set)  # the bearer tokens a login handed out

    def key_is_valid(self, key: object) -> bool:
        return any(k["key"] == key for k in self.auth_keys)

    def _authenticated(self, headers: dict[str, str]) -> bool:
        bearer = headers.get("authorization", "").removeprefix("Bearer ")
        return self.key_is_valid(headers.get("x-api-key")) or bearer in self.sessions

    def _login(self, recorded: RecordedRequest) -> httpx2.Response:
        body = json.loads(recorded.body or b"{}")
        if (body.get("username"), body.get("password")) != self.account:
            return self._text(401, "Unauthorized")
        session = secrets.token_hex(8)
        self.sessions.add(session)
        return self._json({"username": self.account[0], "token": session})

    # -- scenario helpers -------------------------------------------------------------------
    def add_member_key(
        self,
        username: str,
        *,
        roles: tuple[str, ...] = ("Login",),
        age_rating: int | None = 2,
        include_unknowns: bool = False,
    ) -> str:
        """A new key of the account `username`, random each time (a test holds no literal key).
        The account is a restricted reader by default (rating 2: age 3, no unknowns);
        `age_rating=None` leaves the restriction out of `GET /api/Account`, -1 is none."""
        key = secrets.token_urlsafe(24)
        restriction = (
            None if age_rating is None
            else {"ageRating": age_rating, "includeUnknowns": include_unknowns}
        )  # fmt: skip
        self.member_profiles[key] = {"roles": list(roles), "ageRestriction": restriction}
        self.auth_keys.append({"id": 100 + len(self.auth_keys), "name": username, "key": key})
        self.key_accounts[key] = username
        return key

    def requests_with_key(self, key: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.headers.get("x-api-key") == key]

    def demote(self) -> None:
        """The dedicated account loses its administrator role (EF-EXP-009, 403)."""
        self.is_admin = False

    def add_series(
        self,
        name: str,
        folder: str,
        files: list[str],
        *,
        library_id: int = 7,
        name_locked: bool = False,
        age_rating: int = 0,
        age_locked: bool = False,
    ) -> int:
        """A series Kavita has already indexed."""
        self.next_series_id += 1
        sid = self.next_series_id
        self.series.append(
            {
                "id": sid, "name": name, "libraryId": library_id, "folderPath": folder,
                "lowestFolderPath": folder, "nameLocked": name_locked,
                "coverImageLocked": False,
            }
        )  # fmt: skip
        self.files[sid] = list(files)
        self.ages[sid] = {
            "seriesId": sid, "ageRating": age_rating, "ageRatingLocked": age_locked,
            "summary": "", "genres": [], "genresLocked": False, "summaryLocked": False,
            "writers": [], "writerLocked": False,
        }  # fmt: skip
        return sid

    def set_user_cover(self, series_id: int, image: bytes) -> None:
        """A user picked a cover in Kavita: it is kept and the cover is locked."""
        self.covers[series_id] = image
        for series in self.series:
            if series["id"] == series_id:
                series["coverImageLocked"] = True

    def index_after_scans(
        self, name: str, folder: str, files: list[str], *, library_id: int = 7, scans: int = 1
    ) -> None:
        """What is on disk: Kavita sees it once `folder` has been scanned `scans` times."""
        series = {
            "name": name, "libraryId": library_id, "folderPath": folder,
            "lowestFolderPath": folder, "nameLocked": False,
        }  # fmt: skip
        self.pending.append(PendingIndex(series, list(files), scans_needed=scans))

    def discover(self, folder: str, files: list[str], *, library_id: int = 7) -> None:
        """A scan of `folder` found `files`: they join the series that lives there, and only a
        folder no series lives in makes a new one (named as the files read)."""
        for series in self.series:
            if series["libraryId"] == library_id and folder in (
                series["lowestFolderPath"],
                series["folderPath"],
            ):
                known = self.files[series["id"]]  # a path is indexed once
                self.files[series["id"]] = [*known, *(f for f in files if f not in known)]
                return
        name = simulate_parse(files[0].rsplit("/", 1)[-1]).series_name
        self.add_series(name, folder, files, library_id=library_id)

    def reinstall(self, install_id: str, *, libraries: list[dict[str, Any]] | None = None) -> None:
        """Kavita reinstalled from scratch: a new installation, nothing of the old database."""
        self.install_id = install_id
        self.libraries = libraries if libraries is not None else []
        self.reinstalls += 1  # a new database: its first account is new, even if installId is not
        self.users = default_users(f"2026-0{self.reinstalls + 1}-01T00:00:00Z")
        self.series, self.files, self.ages, self.pending = [], {}, {}, []
        self.relations = {}
        self.shelves = KavitaCollections()

    def not_found_resource(self, method: str, path: str) -> None:
        """The route exists but answers 404 with a ProblemDetails body (a resource is missing)."""
        self.problem_404.add((method.upper(), path.lower()))

    def remove_route(self, method: str, path: str) -> None:
        self.missing.add((method.upper(), path.lower()))

    def requests_to(self, fragment: str) -> list[RecordedRequest]:
        return [r for r in self.requests if fragment.lower() in r.path.lower()]

    @property
    def post_reads(self) -> list[RecordedRequest]:
        """POSTs that only read (`all-v2`, `series-by-ids`), kept apart from the writes."""
        return [r for r in self.requests if r.method != "GET" and r not in self.writes]

    @property
    def writes(self) -> list[RecordedRequest]:
        reads = ("parse", "/series/all-v2", "/series/series-by-ids")  # POSTs that only read
        return [
            r
            for r in self.requests
            if r.method != "GET" and not any(part in r.path.lower() for part in reads)
        ]

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    # -- HTTP -------------------------------------------------------------------------------
    def wake_up(self) -> None:
        self.asleep = False

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.asleep:
            raise httpx2.ConnectError("connection refused", request=request)
        url = request.url
        recorded = RecordedRequest(
            request.method,
            url.path,
            {k: v for k, v in url.params.items()},
            {k.lower(): v for k, v in request.headers.items()},
            request.content,
            str(url),
        )
        self.requests.append(recorded)
        if self.hook is not None:
            self.hook(recorded)
        route = (request.method, url.path.lower())
        if route not in ALLOWED:
            return self._violation(f"{request.method} {url.path} is not an allow-listed route")
        if any(name.lower() in {"apikey", "api_key", "key", "token"} for name in recorded.query):
            return self._violation("a credential travels in the URL")
        if route in self.problem_404:
            return self._json({"title": "Not Found", "status": 404}, 404)
        if route in self.missing:
            return self._text(404, "Not Found")
        if route[1] == "/api/account/login":
            return self._login(recorded)
        if not self._authenticated(recorded.headers):
            return self._text(401, "Unauthorized")
        if ALLOWED[route] and not self.is_admin:
            return self._text(403, "Forbidden")
        if route[1].startswith(("/api/collection", "/api/readinglist")):
            return self.shelves.handle(self, recorded)
        return getattr(self, "_" + route[1].rsplit("/", 1)[-1].replace("-", "_"))(recorded)

    def _violation(self, why: str) -> httpx2.Response:
        self.violations.append(why)
        return self._text(400, why)

    @staticmethod
    def _text(status: int, text: str) -> httpx2.Response:
        return httpx2.Response(
            status, content=text.encode(), headers={"content-type": "text/plain"}
        )

    @staticmethod
    def _json(payload: Any, status: int = 200) -> httpx2.Response:
        return httpx2.Response(status, json=payload)

    def _body(self, recorded: RecordedRequest, dto: str) -> dict[str, Any] | httpx2.Response:
        """The JSON body, checked against the DTO of the pinned openapi (no unknown field)."""
        if not recorded.headers.get("content-type", "").startswith("application/json"):
            return self._violation(f"{recorded.path}: the body is not application/json")
        try:
            body = recorded.json()
        except ValueError:
            return self._violation(f"{recorded.path}: the body is not JSON")
        schema = _schema(dto)
        unknown = set(body) - set(schema["properties"])
        missing = set(schema.get("required", [])) - set(body)
        if unknown or missing:
            return self._violation(
                f"{recorded.path}: {dto} unknown={sorted(unknown)} missing={sorted(missing)}"
            )
        assert isinstance(body, dict)
        return body

    # -- routes -----------------------------------------------------------------------------
    def _account(self, recorded: RecordedRequest) -> httpx2.Response:
        """`GET /api/Account`: the `UserDto` of the key, which carries a session token and the
        account's default key (a trap: Shelfhold reads the name and nothing else)."""
        key = recorded.headers.get("x-api-key", "")
        name = self.key_accounts.get(key, self.account[0])
        profile = self.member_profiles.get(key, {"roles": ["Login"], "ageRestriction": None})
        answer = {"id": 1, "username": name, "token": "synthetic-session-token",
                  "apiKey": "synthetic-default-key", "roles": profile["roles"]}  # fmt: skip
        if profile["ageRestriction"] is not None:
            answer["ageRestriction"] = profile["ageRestriction"]
        return self._json(answer)

    # -- the reading progress of a member's account (`ReaderController`, tag v0.9.1.4) -----------
    def _stamp(self) -> str:
        """Kavita's own clock (possibly wrong), the way `LastModifiedUtc` is written."""
        base = self.wall() if self.wall is not None else datetime.now(UTC)
        return (base + self.clock_skew).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _chapter(self, chapter_id: int) -> dict[str, Any] | None:
        for sid, files in self.files.items():
            for i, path in enumerate(files):
                if sid * 100 + i + 1 == chapter_id:
                    library = next((x["libraryId"] for x in self.series if x["id"] == sid), 7)
                    return {"series": sid, "volume": sid * 100, "library": library,
                            "pages": self.chapter_pages.get(path, 20), "path": path}  # fmt: skip
        return None

    def chapter_id(self, path: str) -> int:
        for sid, files in self.files.items():
            if path in files:
                return sid * 100 + files.index(path) + 1
        raise KeyError(path)

    def read_in_kavita(self, account: str, chapter_id: int, page: int) -> None:
        """What Kavita's reader, or an OPDS-PSE client served by it, leaves behind: the page of the
        chapter against the account, stamped with Kavita's clock."""
        self.member_progress[(account, chapter_id)] = {"page": page, "modified": self._stamp()}

    def fail_progress(self, status: int, *, times: int = 1, retry_after: str | None = None) -> None:
        headers = {} if retry_after is None else {"retry-after": retry_after}
        self.progress_failures += [(status, headers)] * times

    def _failing(self) -> httpx2.Response | None:
        if not self.progress_failures:
            return None
        status, headers = self.progress_failures.pop(0)
        return httpx2.Response(status, content=b"synthetic failure", headers=headers)

    def _account_of(self, recorded: RecordedRequest) -> str | None:
        return self.key_accounts.get(recorded.headers.get("x-api-key", ""))

    def _get_progress(self, recorded: RecordedRequest) -> httpx2.Response:
        failing = self._failing()
        if failing is not None:
            return failing
        raw = recorded.query.get("chapterId", "")
        account = self._account_of(recorded)
        chapter = self._chapter(int(raw)) if raw.isdigit() else None
        if account is None or (account, int(raw or 0)) in self.inaccessible:
            return self._text(403, "Forbidden")  # [ChapterAccess]
        if chapter is None:  # a chapter the fake does not know: the stub of "no progress"
            return self._json({"pageNum": 0, "chapterId": 0, "volumeId": 0, "seriesId": 0,
                               "libraryId": 0})  # fmt: skip
        got = self.member_progress.get((account, int(raw)))
        if got is None:  # `GetProgress` answers a stub: page 0, no volume, no series
            return self._json({"pageNum": 0, "chapterId": int(raw), "volumeId": 0, "seriesId": 0,
                               "libraryId": 0})  # fmt: skip
        return self._json(
            {
                "pageNum": got["page"], "chapterId": int(raw), "volumeId": chapter["volume"],
                "seriesId": chapter["series"], "libraryId": chapter["library"],
                "lastModifiedUtc": got["modified"],
            }
        )  # fmt: skip

    def _progress(self, recorded: RecordedRequest) -> httpx2.Response:
        failing = self._failing()
        if failing is not None:
            return failing
        body = self._body(recorded, "ProgressDto")
        if isinstance(body, httpx2.Response):
            return body
        account = self._account_of(recorded)
        chapter = self._chapter(int(body["chapterId"]))
        if account is not None and chapter is None:  # a chapter the fake does not know: a no-op
            return httpx2.Response(200)
        if (
            account is None
            or chapter is None
            or (account, int(body["chapterId"])) in self.inaccessible
        ):
            return self._text(403, "Forbidden")
        if (body["seriesId"], body["volumeId"], body["libraryId"]) != (
            chapter["series"],
            chapter["volume"],
            chapter["library"],
        ):  # a DTO that names another chapter's series: a client bug
            return self._violation("progress: the DTO does not match the chapter")
        page = min(max(int(body["pageNum"]), 0), chapter["pages"])  # CapPageToChapter
        key = (account, int(body["chapterId"]))
        old = self.member_progress.get(key)
        if old is None and page == 0:
            return httpx2.Response(200)  # no empty progress record is created
        if old is not None and old["page"] == page:
            return httpx2.Response(200)  # nothing changes, the date neither
        self.member_progress[key] = {"page": page, "modified": self._stamp()}
        self.progress_writes.append((account, key[1], page))
        return httpx2.Response(200)

    def _continue_point(self, recorded: RecordedRequest) -> httpx2.Response:
        return self._json({"id": 1})

    def _server_info_slim(self, recorded: RecordedRequest) -> httpx2.Response:
        return self._json({"installId": self.install_id, "kavitaVersion": self.version})

    def _authkey_expires(self, recorded: RecordedRequest) -> httpx2.Response:
        expires = self.key_expires_at
        return self._json({"expiresAt": expires.astimezone(UTC).isoformat() if expires else None})

    def _parse(self, recorded: RecordedRequest) -> httpx2.Response:
        library_type = recorded.query.get("libraryType", "")
        if not library_type.isdigit() or int(library_type) not in LIBRARY_TYPES.values():
            return self._violation("plugin/parse without a numeric libraryType")
        if self.manga_parse_only and int(library_type) != LIBRARY_TYPES["Manga"]:
            return self._violation("the replica of plugin/parse is the Manga one only")
        name = recorded.query.get("name", "")
        self.parse_calls.append({"name": name, "libraryType": int(library_type)})
        return self._json(self._parsed(name))

    @staticmethod
    def _parsed(name: str) -> dict[str, Any]:
        """The documented replica of the route (`plugin_parse`): the names a library really has
        (`Vol.1`, `Ch.3 [Team]`, `ch 3`) are read the way Kavita 0.9.1.4 reads them."""
        parsed = simulate_parse(name)
        return {
            "seriesName": parsed.series_name, "seriesYear": parsed.series_year,
            "minChapterNumber": parsed.min_chapter, "maxChapterNumber": parsed.max_chapter,
            "minVolumeNumber": parsed.min_volume, "maxVolumeNumber": parsed.max_volume,
        }  # fmt: skip

    def _parse_bulk(self, recorded: RecordedRequest) -> httpx2.Response:
        body = self._body(recorded, "ParseBulkRequestDto")
        if isinstance(body, httpx2.Response):
            return body
        names = body.get("names") or []
        if len(names) > 100:
            return self._violation("parse-bulk with more than 100 names")
        if body.get("libraryType") not in LIBRARY_TYPES.values():
            return self._violation("parse-bulk without a numeric libraryType")
        if self.manga_parse_only and body["libraryType"] != LIBRARY_TYPES["Manga"]:
            return self._violation("the replica of plugin/parse is the Manga one only")
        self.parse_calls.extend({"name": n, "libraryType": body["libraryType"]} for n in names)
        return self._json({"results": {n: self._parsed(n) for n in names}, "errors": {}})

    def _libraries(self, recorded: RecordedRequest) -> httpx2.Response:
        return self._json(copy.deepcopy(self.libraries))

    def _type(self, recorded: RecordedRequest) -> httpx2.Response:
        found = self._library(recorded.query.get("libraryId", ""))
        return self._json(found["type"]) if found else self._text(400, "unknown library")

    def _library(self, library_id: str | int) -> dict[str, Any] | None:
        return next((lib for lib in self.libraries if str(lib["id"]) == str(library_id)), None)

    @staticmethod
    def _stored(dto: dict[str, Any]) -> dict[str, Any]:
        """UpdateLibraryDto -> LibraryDto (the two name the file types differently)."""
        stored = {k: v for k, v in dto.items() if k != "fileGroupTypes"}
        stored["libraryFileTypes"] = dto["fileGroupTypes"]
        return stored

    def _create(self, recorded: RecordedRequest) -> httpx2.Response:
        body = self._body(recorded, "UpdateLibraryDto")
        if isinstance(body, httpx2.Response):
            return body
        if not self._provider_fits(body["type"], body.get("metadataProvider", 0)):
            return self._invalid_provider()
        created = {**self._stored(body), "id": max([0, *(lib["id"] for lib in self.libraries)]) + 1}
        self.libraries.append(created)
        return self._json(created)

    @staticmethod
    def _provider_fits(library_type: int, provider: int) -> bool:
        return provider in PROVIDERS_FOR_LIBRARY_TYPE.get(library_type, frozenset())

    def _invalid_provider(self) -> httpx2.Response:
        """What Kavita's `ValidateMetadataProvider` answers: a 500 with this message."""
        return self._json({"status": 500, "message": "invalid-metadata-provider"}, 500)

    def _update(self, recorded: RecordedRequest) -> httpx2.Response:
        body = self._body(recorded, "UpdateLibraryDto")
        if isinstance(body, httpx2.Response):
            return body
        found = self._library(body["id"])
        if found is None:
            return self._text(400, "unknown library")
        if not self._provider_fits(found["type"], body.get("metadataProvider", 0)):
            return self._invalid_provider()
        # a replacement, as Kavita does: a field the document omits falls back to its default
        # (read-only attributes of LibraryDto, absent from UpdateLibraryDto, are kept)
        writable = set(_schema("UpdateLibraryDto")["properties"]) | {"libraryFileTypes"}
        kept = {k: v for k, v in found.items() if k not in writable}
        found.clear()
        found.update({**kept, **self._stored(body)})
        return self._text(200, "")

    def _scan_folder(self, recorded: RecordedRequest) -> httpx2.Response:
        body = self._body(recorded, "ScanFolderDto")
        if isinstance(body, httpx2.Response):
            return body
        # Kavita ignores the request (still 200) when the key of the body is not a valid one
        folder = body.get("folderPath") or ""
        valid = self.key_is_valid(body.get("apiKey"))
        (self.scans if valid else self.ignored_scans).append(folder)
        if valid and folder not in self.never_indexed:
            self._reveal(folder)
        return self._text(200, "")

    def _reveal(self, folder: str) -> None:
        for pending in list(self.pending):
            where = pending.series["folderPath"]
            if where == folder or where.startswith(folder.rstrip("/") + "/"):
                pending.scans += 1
                if pending.scans >= pending.scans_needed:
                    self.pending.remove(pending)
                    self.add_series(
                        pending.series["name"], where, pending.files,
                        library_id=pending.series["libraryId"],
                    )  # fmt: skip
                    self.series[-1]["nameLocked"] = pending.series["nameLocked"]

    def _users(self, recorded: RecordedRequest) -> httpx2.Response:
        return self._json(copy.deepcopy(self.users))

    def _all_v2(self, recorded: RecordedRequest) -> httpx2.Response:
        body = self._body(recorded, "SeriesFilterV2Dto")
        if isinstance(body, httpx2.Response):
            return body
        number, size = recorded.query.get("PageNumber", ""), recorded.query.get("PageSize", "")
        if not (number.isdigit() and size.isdigit()):
            return self._violation("all-v2 without PageNumber and PageSize in the URL")
        wanted = {
            int(st["value"])
            for st in body.get("statements") or []
            if st.get("field") == 19 and st.get("comparison") == 0
        }  # Libraries, Equal
        found = [s for s in self.series if not wanted or s["libraryId"] in wanted]
        tags = {
            int(st["value"])
            for st in body.get("statements") or []
            if st.get("field") == 7 and st.get("comparison") == 0
        }  # CollectionTags, Equal: the series of a collection of the key's account
        if tags:
            account = self.key_accounts.get(recorded.headers.get("x-api-key", ""), self.account[0])
            held = {
                s
                for t in tags
                for s in self.shelves.collections.get(t, {"series": []})["series"]
                if self.shelves.collections.get(t, {}).get("owner") == account
            }
            found = [s for s in found if s["id"] in held]
        start = (int(number) - 1) * int(size)
        page = copy.deepcopy(found[start : start + int(size)])
        key = recorded.headers.get("x-api-key", "")
        if key in self.member_profiles:  # `PagesRead` and `LatestReadDate` are the key's account's
            account = self.key_accounts[key]
            for item in page:
                mine = [
                    self.member_progress[(account, self.chapter_id(p))]
                    for p in self.files.get(item["id"], [])
                    if (account, self.chapter_id(p)) in self.member_progress
                ]
                item["pagesRead"] = sum(int(m["page"]) for m in mine)
                item["latestReadDate"] = max(
                    (str(m["modified"]) for m in mine), default="0001-01-01"
                )
        return self._json(page)

    def _series_by_ids(self, recorded: RecordedRequest) -> httpx2.Response:
        body = self._body(recorded, "SeriesByIdsDto")
        if isinstance(body, httpx2.Response):
            return body
        ids = set(body.get("seriesIds") or [])
        return self._json(copy.deepcopy([s for s in self.series if s["id"] in ids]))

    def _series_detail(self, recorded: RecordedRequest) -> httpx2.Response:
        sid = recorded.query.get("seriesId", "")
        if not sid.isdigit() or int(sid) not in self.files:
            return self._json({"title": "Not Found", "status": 404}, 404)
        volume_id = int(sid) * 100
        chapters = [
            {
                "id": volume_id + i + 1,
                "volumeId": volume_id,
                "pages": self.chapter_pages.get(p, 20),
                "pagesRead": self.progress.get(p, 0),
                "files": [{"id": i, "filePath": p}],
            }
            for i, p in enumerate(self.files[int(sid)])
        ]
        # `GetSeriesDetail` lists a numbered volume's chapters in `Chapters` too (0.9.1)
        return self._json(
            {"specials": [], "chapters": copy.deepcopy(chapters),
             "volumes": [{"id": volume_id, "chapters": chapters}]}
        )  # fmt: skip

    def _metadata(self, recorded: RecordedRequest) -> httpx2.Response:
        if recorded.method == "POST":
            return self._update_metadata(recorded)
        sid = recorded.query.get("seriesId", "")
        if not sid.isdigit() or int(sid) not in self.ages:
            return self._json({"title": "Not Found", "status": 404}, 404)
        return self._json(copy.deepcopy(self.ages[int(sid)]))

    def _update_metadata(self, recorded: RecordedRequest) -> httpx2.Response:
        """`UpdateSeriesMetadataDto`: the document replaces the stored one, as Kavita applies it
        (a field the document leaves out is not kept: a faithful update sends them all)."""
        body = self._body(recorded, "UpdateSeriesMetadataDto")
        if isinstance(body, httpx2.Response):
            return body
        document = body.get("seriesMetadata")
        if not isinstance(document, dict):
            return self._violation("metadata update without seriesMetadata")
        unknown = set(document) - set(_schema("SeriesMetadataDto")["properties"])
        sid = document.get("seriesId")
        if unknown or sid not in self.ages:
            return self._violation(f"metadata update: unknown={sorted(unknown)} series={sid!r}")
        self.ages[sid] = copy.deepcopy(document)
        if self.drops_age_lock:
            self.ages[sid]["ageRatingLocked"] = False
        return self._text(200, "")

    def _all_related(self, recorded: RecordedRequest) -> httpx2.Response:
        """`RelatedSeriesDto`: each relation as the series it names (here, its id and its name)."""
        sid = recorded.query.get("seriesId", "")
        if not sid.isdigit() or int(sid) not in self.ages:
            return self._json({"title": "Not Found", "status": 404}, 404)
        held = self.relations.get(int(sid), {})
        document: dict[str, Any] = {"sourceSeriesId": int(sid)}
        for kind in _schema("RelatedSeriesDto")["properties"]:
            if kind != "sourceSeriesId":
                document[kind] = [self._related_series(i) for i in held.get(kind, [])]
        return self._json(document)

    def _related_series(self, series_id: int) -> dict[str, Any]:
        found = next((s for s in self.series if s["id"] == series_id), None)
        return {"id": series_id, "name": found["name"] if found else f"series-{series_id}"}

    def _update_related(self, recorded: RecordedRequest) -> httpx2.Response:
        """`UpdateRelatedSeriesDto`: Kavita's DTO starts every list empty and applies each one as
        it is, so a list the document leaves out is emptied: a faithful update sends them all."""
        body = recorded.json() if recorded.body else None
        if not isinstance(body, dict):
            return self._violation("related update without a JSON object")
        kinds = set(_schema("UpdateRelatedSeriesDto")["properties"]) - {"seriesId"}
        sid = body.get("seriesId")
        if set(body) - kinds - {"seriesId"} or sid not in self.ages:
            return self._violation(f"related update: unknown fields or series {sid!r}")
        replaced: dict[str, list[int]] = {}
        for kind in kinds:
            ids = body.get(kind) or []
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                return self._violation(f"related update: {kind} is not a list of ids")
            if sid in ids or any(i not in self.ages for i in ids):
                return self._violation(f"related update: {kind} names itself or an unknown series")
            replaced[kind] = sorted(set(ids))
        self.relations[sid] = replaced
        self.related_posts.append(copy.deepcopy(body))
        return self._text(200, "")

    def _series(self, recorded: RecordedRequest) -> httpx2.Response:
        body = self._body(recorded, "UploadCoverFileDto")
        if isinstance(body, httpx2.Response):
            return body
        url = body.get("url") or ""
        if url.startswith("data:"):
            return self._violation("cover upload with a data: prefix (Kavita wants bare base64)")
        try:
            self.covers[body["id"]] = base64.b64decode(url, validate=True)
            for series in self.series:
                if series["id"] == body["id"]:
                    series["coverImageLocked"] = bool(body.get("lockCover"))
        except (binascii.Error, ValueError):
            return self._violation("cover upload is not base64")
        return self._text(200, "")


def build_fetcher(fake: FakeKavita, *, base_url: str = BASE_URL) -> IntegrationFetcher:
    """The production IntegrationFetcher, its only reachable host being the simulated Kavita."""
    host = httpx2.URL(base_url).host
    resolver = FakeResolver({host: [LAN_ADDRESS]})
    port = httpx2.URL(base_url).port or 80
    lan = asyncio.run(LanTargets.declare([f"{host}:{port}"], resolver))
    return IntegrationFetcher(resolver, lan_targets=lan, transport=fake.transport)
