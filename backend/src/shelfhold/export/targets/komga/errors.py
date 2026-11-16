"""Failures of the Komga destination. None of them ever carries the key or a response body.

The classes of the destination that mean the same as Kavita's (`TargetUnreachable`,
`TargetForbidden`, `ProtocolError`) are those of `export.targets.kavita.errors`: the pause, the
exit and the code are the same, so the callers catch one family (`TargetError`).
"""

from __future__ import annotations

from shelfhold.export.targets.kavita.errors import TargetError


class KomgaRouteNotAllowed(TargetError):
    """A call outside `docs/destinations/komga-routes.md`: a bug or a hostile plugin, never sent."""

    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"{method} {path[:80]!r} is not an allow-listed Komga route")


class KomgaNoLibrary(TargetError):
    """No library of the server holds this folder: nothing is scanned (never every library)."""
