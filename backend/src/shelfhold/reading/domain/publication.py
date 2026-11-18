"""What a reader opens of a book, as plain values (M5 L06, EF-LEC-014, ADR-0055).

A publication is the list the reader walks: the pages of an image carrier, in reading order, or the
reading order of an EPUB; each entry is a name of the archive the server chose, never a path from a
caller. The direction of reading is the Work's own, else its package's, else the default of its
kind; a webtoon is the `vertical` of the catalogue and is called `webtoon` here.
"""

from __future__ import annotations

import posixpath
from typing import Final, Literal
from urllib.parse import unquote, urlsplit

from shelfhold.reading.domain.content_types import media_type_of, safe_entry
from shelfhold.reading.domain.excerpt import natural_key

__all__ = [
    "PAGE_TYPES",
    "ReadingDirection",
    "page_names",
    "reading_direction",
    "resolve_in_package",
]

ReadingDirection = Literal["ltr", "rtl", "webtoon"]

PAGE_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"})
_NAMES: Final = {"ltr": "ltr", "rtl": "rtl", "vertical": "webtoon", "webtoon": "webtoon"}


def reading_direction(
    work: str | None, package: str | None = None, kind_default: str | None = None
) -> ReadingDirection:
    """The Work's own direction wins (the series), then the progression of the package, then the
    default of the kind of Work; a book that says nothing reads left to right."""
    for candidate in (work, package, kind_default):
        if candidate in _NAMES:
            return _NAMES[candidate]  # type: ignore[return-value]
    return "ltr"


def page_names(names: frozenset[str]) -> list[str]:
    """The pages of an image carrier among the names of its archive, `p2` before `p10`. A name that
    `/content` would refuse (a climbing or odd name) is no page: it could never be served."""
    return sorted(
        (n for n in names if media_type_of(n) in PAGE_TYPES and safe_entry(n)), key=natural_key
    )


def resolve_in_package(package: str, href: str, names: frozenset[str]) -> str | None:
    """`href` as written in the package `package`, as an entry of the archive; `None` for anything
    that is not a plain relative name that is there (a scheme, a host, an absolute path)."""
    value = href.strip()
    parts = urlsplit(value)
    if not value or parts.scheme or parts.netloc or value.startswith(("/", "#")):
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(package), unquote(parts.path)))
    return joined if joined in names else None
