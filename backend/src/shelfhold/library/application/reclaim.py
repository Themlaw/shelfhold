"""The rules of « Récupérer de la place » (EF-BIB-030) that need no disk and no database.

- The word typed to empty the trash (a reserved action: the server compares it, whatever the door).
- The order of the early purge: oldest first, and which entries may be taken at all.

What removes (and what keeps) is `export.application.reclaim`; fsops enforces the perimeter, the
minimum age and the pressure again, from the file system.
"""

import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

__all__ = [
    "EARLY_PURGE_BATCH",
    "EMPTY_TRASH_PHRASE",
    "BinCandidate",
    "TrashConfirmationError",
    "oldest_first",
    "require_empty_phrase",
]

EMPTY_TRASH_PHRASE: Final = "empty-trash"
EARLY_PURGE_BATCH: Final = 50  # entries one run of the early purge may take at most


class TrashConfirmationError(ValueError):
    """The typed word is not the phrase: nothing was removed."""

    code = "trash.confirmation_required"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: type {EMPTY_TRASH_PHRASE} to confirm")


def require_empty_phrase(typed: str | None) -> None:
    if typed is None or not hmac.compare_digest(
        typed.strip().encode(), EMPTY_TRASH_PHRASE.encode()
    ):
        raise TrashConfirmationError


@dataclass(frozen=True)
class BinCandidate:
    root: str
    name: str  # the entry, `<root>/.shelfhold/recycle/<name>`
    term: datetime | None  # the term the deletion gave it (`purge_after`); None: not in the index


def oldest_first(candidates: Iterable[BinCandidate]) -> list[BinCandidate]:
    """Oldest deletion first (the term is the deletion plus a constant). An entry the index does
    not know goes last (fsops reads its age from the file system, which is what decides), then
    by name, for a stable order."""
    items = list(candidates)
    known = [c for c in items if c.term is not None]
    unknown = [c for c in items if c.term is None]
    known.sort(key=lambda c: (c.term.replace(tzinfo=None), c.root, c.name))  # type: ignore[union-attr]
    unknown.sort(key=lambda c: (c.root, c.name))
    return known + unknown
