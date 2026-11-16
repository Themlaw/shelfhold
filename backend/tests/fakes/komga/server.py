"""A simulated Komga server, in process, synthetic only (EF-EXP-020, ENF-LEG-013).

It answers the routes of `docs/destinations/komga-routes.md` the way the API of Komga 1.x
describes them (an API key in `X-API-Key`, a library scan and an `empty-trash` that answer 202 and
take no body) and plays the host's referee: any request outside its own allow-list, a credential
in a URL, a body on a route that takes none, a path that names a folder after `/scan`, is
answered 400 and recorded in `violations`. The allow-list is written here on purpose,
independently of the adapter under test.

The simulated server is wired to the real `IntegrationFetcher` (declared LAN target, scripted
DNS, mock transport), so the SSRF guard and the header rules are exercised as in production.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

import httpx2

from shelfhold.infrastructure.fetcher import IntegrationFetcher, LanTargets
from tests.fakes_fetcher import FakeResolver
from tests.redaction_support import synthetic_text

BASE_URL = "http://komga.lan:25600"
LAN_ADDRESS = "192.168.1.60"
API_KEY = synthetic_text("komga-fake-administrator")  # built at run time: no literal secret
OTHER_KEY = synthetic_text("komga-fake-reader")  # an account that is not an administrator
UNKNOWN_KEY = synthetic_text("komga-fake-revoked")  # a key the server does not know

# (method, path pattern) -> needs an administrator. Written here, not imported.
ALLOWED: dict[tuple[str, str], bool] = {
    ("GET", r"/actuator/info"): True,
    ("GET", r"/api/v2/users/me"): False,
    ("GET", r"/api/v2/users"): True,
    ("GET", r"/api/v1/libraries"): False,
    ("POST", r"/api/v1/libraries/(?P<library>[A-Za-z0-9]+)/scan"): True,
    ("POST", r"/api/v1/libraries/(?P<library>[A-Za-z0-9]+)/empty-trash"): True,
}
CREDENTIAL_PARAMS = {"apikey", "api_key", "key", "token", "access_token", "password"}


def default_libraries() -> list[dict[str, Any]]:
    """Two libraries, one root each, with names that are not a third party's."""
    return [
        {"id": "0SYNTHMANGA01", "name": "Synthetic Manga", "root": "/manga", "unavailable": False},
        {
            "id": "0SYNTHCOMIC02",
            "name": "Synthetic Comics",
            "root": "/comics",
            "unavailable": False,
        },
    ]


def administrator() -> dict[str, Any]:
    return {"id": "0SYNTHUSER001", "email": "service@komga.invalid", "roles": ["ADMIN"],
            "sharedAllLibraries": True, "sharedLibrariesIds": [], "labelsAllow": [],
            "labelsExclude": [], "ageRestriction": None}  # fmt: skip


def reader(
    name: str, libraries: list[str], *, age: int | None = None, restriction: str = "ALLOW_ONLY",
    roles: list[str] | None = None, shared_all: bool = False,
) -> dict[str, Any]:  # fmt: skip
    """A non-administrator account: the libraries it is shared, an age restriction or none."""
    return {"id": f"0SYNTH{name.upper()}", "email": f"{name}@komga.invalid",
            "roles": roles if roles is not None else ["USER"], "sharedAllLibraries": shared_all,
            "sharedLibrariesIds": list(libraries), "labelsAllow": [], "labelsExclude": [],
            "ageRestriction": None if age is None else {"age": age, "restriction": restriction}}  # fmt: skip


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes
    raw_url: str


@dataclass
class FakeKomga:
    version: str = "1.22.1"
    libraries: list[dict[str, Any]] = field(default_factory=default_libraries)
    users: list[dict[str, Any]] = field(default_factory=lambda: [administrator()])
    keys: dict[str, bool] = field(default_factory=lambda: {API_KEY: True, OTHER_KEY: False})
    asleep: bool = False
    requests: list[RecordedRequest] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    scans: list[str] = field(default_factory=list)  # library ids, in the order Komga was asked
    emptied: list[str] = field(default_factory=list)
    order: list[tuple[str, str]] = field(default_factory=list)  # ("scan"|"empty-trash", library)

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def requests_to(self, fragment: str) -> list[RecordedRequest]:
        return [r for r in self.requests if fragment in r.path]

    def requests_with_key(self, key: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.headers.get("x-api-key") == key]

    def _json(self, status: int, payload: Any) -> httpx2.Response:
        return httpx2.Response(status, json=payload)

    def _violation(self, why: str) -> httpx2.Response:
        self.violations.append(why)
        return httpx2.Response(400, json={"error": "Bad Request", "message": why})

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.asleep:
            raise httpx2.ConnectError("connection refused", request=request)
        url = request.url
        recorded = RecordedRequest(
            request.method, url.path, dict(url.params.items()),
            {k.lower(): v for k, v in request.headers.items()}, request.content, str(url),
        )  # fmt: skip
        self.requests.append(recorded)
        matched = next(
            (
                (needs_admin, found)
                for (method, pattern), needs_admin in ALLOWED.items()
                if method == request.method and (found := re.fullmatch(pattern, url.path))
            ),
            None,
        )
        if matched is None:
            return self._violation(f"{request.method} {url.path} is not an allow-listed route")
        if any(name.lower() in CREDENTIAL_PARAMS for name in recorded.query):
            return self._violation("a credential travels in the URL")
        if set(recorded.query) - {"deep"}:
            return self._violation("a parameter the route does not declare")
        if request.method == "POST" and request.content.strip():
            return self._violation("this route takes no body")
        key = recorded.headers.get("x-api-key", "")
        if key not in self.keys:
            return self._json(401, {"error": "Unauthorized"})
        needs_admin, found = matched
        if needs_admin and not self.keys[key]:
            return self._json(403, {"error": "Forbidden"})
        return self._answer(url.path, found)

    def _answer(self, path: str, found: re.Match[str]) -> httpx2.Response:
        if path == "/actuator/info":
            return self._json(200, {"build": {"name": "Komga", "version": self.version}})
        if path == "/api/v2/users/me":
            return self._json(
                200,
                {"id": "0SYNTHUSER001", "email": "service@komga.invalid", "roles": ["ADMIN"],
                 "sharedAllLibraries": True, "sharedLibrariesIds": []},
            )  # fmt: skip
        if path == "/api/v2/users":
            return self._json(200, [dict(user) for user in self.users])
        if path == "/api/v1/libraries":
            return self._json(200, [dict(lib) for lib in self.libraries])
        library = found.group("library")
        if library not in {lib["id"] for lib in self.libraries}:
            return self._json(404, {"error": "Not Found"})
        action = "scan" if path.endswith("/scan") else "empty-trash"
        (self.scans if action == "scan" else self.emptied).append(library)
        self.order.append((action, library))
        return httpx2.Response(202)


def build_fetcher(fake: FakeKomga, *, base_url: str = BASE_URL) -> IntegrationFetcher:
    """The production IntegrationFetcher, its only reachable host being the simulated Komga."""
    host = httpx2.URL(base_url).host
    resolver = FakeResolver({host: [LAN_ADDRESS]})
    port = httpx2.URL(base_url).port or 80
    lan = asyncio.run(LanTargets.declare([f"{host}:{port}"], resolver))
    return IntegrationFetcher(resolver, lan_targets=lan, transport=fake.transport)
