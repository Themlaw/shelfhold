"""The allow-list of Komga routes: `docs/destinations/komga-routes.md` says the same thing.

A test compares the two. Anything else is refused before a request is built. Komga has no scan of
a folder: the unit it rescans is the library, so no route of this list names a folder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from shelfhold.export.targets.komga.errors import KomgaRouteNotAllowed
from shelfhold.sdk.api_call import Method

LIBRARY_ID: Final = re.compile(r"[A-Za-z0-9]{1,64}", re.ASCII)
_PARAMETER: Final = "{library_id}"


@dataclass(frozen=True)
class Route:
    method: Method
    path: str  # the canonical spelling, `{library_id}` for the one identifier it takes
    write: bool  # changes Komga (never while the destination is paused)
    admin: bool  # Komga answers 403 to an account that is not an administrator

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"

    def matches(self, path: str) -> str | None:
        """The library id the path names (`""` for a route without one), or None."""
        if _PARAMETER not in self.path:
            return "" if path == self.path else None
        head, tail = self.path.split(_PARAMETER)
        if not (path.startswith(head) and path.endswith(tail)):
            return None
        found = path[len(head) : len(path) - len(tail)]
        return found if LIBRARY_ID.fullmatch(found) else None


INFO: Final = Route("GET", "/actuator/info", write=False, admin=True)
ME: Final = Route("GET", "/api/v2/users/me", write=False, admin=False)
LIBRARIES: Final = Route("GET", "/api/v1/libraries", write=False, admin=False)
LIBRARY_SCAN: Final = Route("POST", "/api/v1/libraries/{library_id}/scan", write=True, admin=True)
LIBRARY_EMPTY_TRASH: Final = Route(
    "POST", "/api/v1/libraries/{library_id}/empty-trash", write=True, admin=True
)
ALLOWED_ROUTES: Final = (INFO, ME, LIBRARIES, LIBRARY_SCAN, LIBRARY_EMPTY_TRASH)
SCAN_QUERY: Final = frozenset({"deep"})  # `deep=true` rereads files Komga thinks unchanged


def resolve(method: str, path: str) -> tuple[Route, str]:
    """The listed route for this call and the library it names, matched exactly."""
    for route in ALLOWED_ROUTES:
        if route.method == method.upper() and (library := route.matches(path)) is not None:
            return route, library
    raise KomgaRouteNotAllowed(method, path)
