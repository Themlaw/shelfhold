"""The allow-list of Kavita routes (EF-EXP-009): `docs/kavita-routes.md` says the same thing.

A test compares the two and the frozen `openapi.json` of every version of the range. Anything
else is refused before a request is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from shelfhold.export.targets.kavita.errors import RouteNotAllowed


@dataclass(frozen=True)
class Route:
    method: str
    path: str  # canonical spelling of the openapi
    write: bool  # changes Kavita (refused in degraded_readonly)

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


SERVER_INFO: Final = Route("GET", "/api/Server/server-info-slim", write=False)
KEY_EXPIRES: Final = Route("GET", "/api/Plugin/authkey-expires", write=False)
PARSE: Final = Route("GET", "/api/Plugin/parse", write=False)
PARSE_BULK: Final = Route("POST", "/api/Plugin/parse-bulk", write=False)  # reads, despite the POST
LIBRARIES: Final = Route("GET", "/api/Library/libraries", write=False)
LIBRARY_TYPE: Final = Route("GET", "/api/Library/type", write=False)
LIBRARY_CREATE: Final = Route("POST", "/api/Library/create", write=True)
LIBRARY_UPDATE: Final = Route("POST", "/api/Library/update", write=True)
SCAN_FOLDER: Final = Route("POST", "/api/Library/scan-folder", write=True)
UPLOAD_SERIES_COVER: Final = Route("POST", "/api/Upload/series", write=True)

ALLOWED_ROUTES: Final = (
    SERVER_INFO, KEY_EXPIRES, PARSE, PARSE_BULK, LIBRARIES, LIBRARY_TYPE, LIBRARY_CREATE,
    LIBRARY_UPDATE, SCAN_FOLDER, UPLOAD_SERIES_COVER,
)  # fmt: skip
_BY_KEY: Final = {(r.method, r.path.lower()): r for r in ALLOWED_ROUTES}
# Routes that need an administrator, so that a 403 means "the account was demoted".
PARSE_ROUTES: Final = frozenset({PARSE.label, PARSE_BULK.label})


def resolve(method: str, path: str) -> Route:
    """The listed route for this call, matched exactly (the case of the path does not matter)."""
    route = _BY_KEY.get((method.upper(), path.lower()))
    if route is None:
        raise RouteNotAllowed(method, path)
    return route
