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
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from shelfhold.infrastructure.fetcher import IntegrationFetcher, LanTargets
from tests.fakes_fetcher import FakeResolver

TESTS = Path(__file__).resolve().parents[2]
OPENAPI = TESTS / "fixtures" / "kavita" / "openapi"
BASE_URL = "http://kavita.lan:5000"
LAN_ADDRESS = "192.168.1.50"
API_KEY = "synthetic-admin-key-0001"

# (method, lower-cased path) -> needs an administrator. Written here, not imported.
ALLOWED: dict[tuple[str, str], bool] = {
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
}
LIBRARY_TYPES = {"Manga": 0, "Comic": 1, "Book": 2, "Image": 3, "LightNovel": 4, "ComicVine": 5}
_NAME = re.compile(r"^(?P<series>.+?) - (?:Vol\.(?P<vol>[\d.]+) )?(?:Ch\.(?P<ch>[\d.]+))?")


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
            "inheritWebLinksFromFirstChapter": True, "defaultLanguage": "ja", "metadataProvider": 1,
        },
        {
            "id": 8, "name": "Synthetic Comics", "type": 1, "folders": ["/comics"],
            "folderWatching": True, "includeInDashboard": True, "includeInRecommended": True,
            "manageCollections": True, "manageReadingLists": True, "includeInSearch": True,
            "allowScrobbling": True, "collapseSeriesRelationships": False,
            "libraryFileTypes": [1], "excludePatterns": [], "allowMetadataMatching": True,
            "enableMetadata": True, "removePrefixForSortName": False,
            "inheritWebLinksFromFirstChapter": False, "defaultLanguage": "", "metadataProvider": 0,
        },
    ]  # fmt: skip


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
    requests: list[RecordedRequest] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    scans: list[str] = field(default_factory=list)
    ignored_scans: list[str] = field(default_factory=list)
    covers: dict[int, bytes] = field(default_factory=dict)
    parse_calls: list[dict[str, Any]] = field(default_factory=list)

    # -- scenario helpers -------------------------------------------------------------------
    def demote(self) -> None:
        """The dedicated account loses its administrator role (EF-EXP-009, 403)."""
        self.is_admin = False

    def remove_route(self, method: str, path: str) -> None:
        self.missing.add((method.upper(), path.lower()))

    def requests_to(self, fragment: str) -> list[RecordedRequest]:
        return [r for r in self.requests if fragment.lower() in r.path.lower()]

    @property
    def writes(self) -> list[RecordedRequest]:
        return [r for r in self.requests if r.method != "GET" and "parse" not in r.path.lower()]

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    # -- HTTP -------------------------------------------------------------------------------
    def handle(self, request: httpx2.Request) -> httpx2.Response:
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
        route = (request.method, url.path.lower())
        if route not in ALLOWED:
            return self._violation(f"{request.method} {url.path} is not an allow-listed route")
        if any(name.lower() in {"apikey", "api_key", "key", "token"} for name in recorded.query):
            return self._violation("a credential travels in the URL")
        if route in self.missing:
            return self._text(404, "Not Found")
        if recorded.headers.get("x-api-key") != API_KEY:
            return self._text(401, "Unauthorized")
        if ALLOWED[route] and not self.is_admin:
            return self._text(403, "Forbidden")
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
    def _server_info_slim(self, recorded: RecordedRequest) -> httpx2.Response:
        return self._json({"installId": self.install_id, "kavitaVersion": self.version})

    def _authkey_expires(self, recorded: RecordedRequest) -> httpx2.Response:
        expires = self.key_expires_at
        return self._json({"expiresAt": expires.astimezone(UTC).isoformat() if expires else None})

    def _parse(self, recorded: RecordedRequest) -> httpx2.Response:
        library_type = recorded.query.get("libraryType", "")
        if not library_type.isdigit() or int(library_type) not in LIBRARY_TYPES.values():
            return self._violation("plugin/parse without a numeric libraryType")
        name = recorded.query.get("name", "")
        self.parse_calls.append({"name": name, "libraryType": int(library_type)})
        return self._json(self._parsed(name))

    @staticmethod
    def _parsed(name: str) -> dict[str, Any]:
        match = _NAME.match(name)
        chapter = float(match["ch"]) if match and match["ch"] else 0.0
        volume = float(match["vol"]) if match and match["vol"] else 0.0
        return {
            "seriesName": match["series"] if match else name, "seriesYear": None,
            "minChapterNumber": chapter, "maxChapterNumber": chapter,
            "minVolumeNumber": volume, "maxVolumeNumber": volume,
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
        created = {**self._stored(body), "id": max([0, *(lib["id"] for lib in self.libraries)]) + 1}
        self.libraries.append(created)
        return self._json(created)

    def _update(self, recorded: RecordedRequest) -> httpx2.Response:
        body = self._body(recorded, "UpdateLibraryDto")
        if isinstance(body, httpx2.Response):
            return body
        found = self._library(body["id"])
        if found is None:
            return self._text(400, "unknown library")
        found.update(self._stored(body))
        return self._text(200, "")

    def _scan_folder(self, recorded: RecordedRequest) -> httpx2.Response:
        body = self._body(recorded, "ScanFolderDto")
        if isinstance(body, httpx2.Response):
            return body
        # Kavita ignores the request (still 200) when the key of the body is not a valid one
        (self.scans if body.get("apiKey") == API_KEY else self.ignored_scans).append(
            body.get("folderPath") or ""
        )
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
