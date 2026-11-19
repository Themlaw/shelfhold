"""The bound of « Lire un extrait » (ADR-0055 D-P1, N3), as pure functions.

An excerpt is **computed by the server**, from the file it is going to serve, never given by a
caller. Pages: the first `EXCERPT_PAGES` of the sorted list, and never more than `EXCERPT_RATIO` of
it. A reading order (an EPUB): the first documents whose sizes add up to at most `EXCERPT_RATIO` of
the whole; when not even the first one fits, or the book has a single document, there is nothing to
cut at a document boundary and the answer is `None`: « extrait indisponible », never the whole file.
(Cutting a document in two, a derivative made for the excerpt, comes later.)
"""

import re
from collections.abc import Sequence
from typing import Final

EXCERPT_PAGES: Final = 10
EXCERPT_RATIO: Final = 0.2
_DIGITS = re.compile(r"(\d+)")


def natural_key(name: str) -> list[object]:
    """`p2` before `p10`: digits compared as numbers."""
    return [int(part) if part.isdigit() else part.lower() for part in _DIGITS.split(name)]


def page_excerpt(pages: Sequence[str]) -> list[str] | None:
    """The first pages, at most `EXCERPT_PAGES` and at most `EXCERPT_RATIO` of all of them (rounded
    down); `None` when that is not even one page."""
    ordered = sorted(pages, key=natural_key)
    count = min(EXCERPT_PAGES, int(len(ordered) * EXCERPT_RATIO))
    return ordered[:count] if count >= 1 else None


def spine_excerpt(spine: Sequence[tuple[str, int]]) -> list[str] | None:
    """The documents of a reading order `(entry, size)`, from the first, while they add up to at
    most `EXCERPT_RATIO` of the total; `None` when the first alone is more, or there is one only."""
    total = sum(size for _entry, size in spine)
    if len(spine) < 2 or total <= 0:
        return None
    taken: list[str] = []
    used = 0
    for entry, size in spine:
        if (used + size) > total * EXCERPT_RATIO:
            break
        used += size
        taken.append(entry)
    return taken or None
