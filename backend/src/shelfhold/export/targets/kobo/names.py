"""The names of the Kobo folder (EF-EXP-021, D19): a file per book, the extension a Kobo wants.

A Kobo reads a file whose name ends in `.kepub.epub` — the extension is what makes the reader give
the book its spans, so it is mandatory, never `.epub` alone. The name follows the house convention
of a tree: the number first, zero-padded (`Ch.0012`, `Vol.0003`), the title after a space, one
component, never hidden, never longer than a file name may be. Pure.
"""

from decimal import Decimal, InvalidOperation
from typing import Final

from shelfhold.export.domain.naming import MAX_COMPONENT_BYTES, sanitize_name

__all__ = ["KEPUB_EXT", "book_filename", "series_folder"]

KEPUB_EXT: Final = ".kepub.epub"  # the extension a Kobo wants, mandatory (EF-EXP-021)
_MIN_DIGITS: Final = 4


def _number(number: str | None) -> str | None:
    """`12` -> `0012`, `12.50` -> `0012.5`; None for no number (or one that is not a decimal)."""
    if not number:
        return None
    try:
        value = Decimal(number)
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    whole, _, fraction = format(value.normalize(), "f").partition(".")
    return f"{whole.zfill(_MIN_DIGITS)}.{fraction}" if fraction else whole.zfill(_MIN_DIGITS)


def book_filename(number: str | None, title: str, *, volume: bool = False) -> str:
    """`Ch.0012 Dawn.kepub.epub`: one book, one file, always the Kobo extension. A volume that
    holds several units is `Vol.0003 Title.kepub.epub`; a book with no number is its title."""
    shown = _number(number)
    label = "Vol" if volume else "Ch"
    prefix = f"{label}.{shown}" if shown is not None else ""
    room = MAX_COMPONENT_BYTES - len(prefix.encode()) - len(KEPUB_EXT.encode()) - 1
    clean = sanitize_name(title, max_bytes=max(8, room)) if title.strip() else ""
    if clean == "_" and not title.strip(" ._"):
        clean = ""
    stem = " ".join(part for part in (prefix, clean) if part) or "Item"
    return f"{stem}{KEPUB_EXT}"


def series_folder(name: str) -> str:
    """The folder of a series: one component, never hidden, never reserved."""
    return sanitize_name(name)
