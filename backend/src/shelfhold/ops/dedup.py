"""The suggestions of duplicate Works on the admin door (EF-BIB-004).

`scan` looks for pairs that share a title and a credited person, and for a series that moved
sources in Suwayomi (EF-BIB-029), and records them as suggestions;
`suggestions` lists the open ones. Nothing is merged here: a merge is the admin's own decision,
through the merge plan. The Work tombstones are asked first (fails closed: with no screen there
is no scan), before any write transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from shelfhold.catalog.public import (
    TombstoneScreen,
    detect_suwayomi_migrations,
    open_suggestions,
    suggest_duplicates,
)
from shelfhold.infrastructure.db import write_transaction
from shelfhold.ops.errors import ExitUnavailableError

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = ["DedupOps"]


class Screens(Protocol):
    def open(self) -> TombstoneScreen: ...


class DedupOps:
    def __init__(self, engine: Engine, screens: Screens | None = None) -> None:
        self._engine = engine
        self._screens = screens

    def scan(self) -> dict[str, Any]:
        if self._screens is None:
            raise ExitUnavailableError("the Work tombstones cannot be checked here")
        screen = self._screens.open()  # opened before the write transaction: the replay may write
        with write_transaction(self._engine) as connection:
            created = suggest_duplicates(connection, screen=screen)
            # a series that moved sources in Suwayomi: "Même œuvre ?", never a creation
            created += detect_suwayomi_migrations(connection, screen=screen)
        return {"created": created}

    def suggestions(self) -> list[dict[str, str]]:
        with self._engine.connect() as connection:
            return [
                {"id": s.id, "a": s.a, "b": s.b, "reason": s.reason, "state": s.state}
                for s in open_suggestions(connection)
            ]
