"""The names of the local layout of Mihon (EF-EXP-010), read in its `LocalSource` and
`ChapterRecognition`: a file `Ch.<number> <title>.<ext>` per chapter, in a folder per series.

Mihon reads the chapter number in the file name behind `ch.` (`(?<=ch\\.) *([0-9]+)(\\.[0-9]+)?`),
so the number is the first thing the name says, zero-padded to four digits (`{Chapter:0000.#}`):
`Ch.0012`, `Ch.0012.5`. The title follows after a space, and is left out when there is none. A file
that starts with a dot is hidden for Mihon, and a `*.json` in a series folder is taken for its
legacy details file and deleted: no name made here is either. Pure.
"""

from decimal import Decimal, InvalidOperation
from typing import Final

from shelfhold.export.domain.naming import MAX_COMPONENT_BYTES, sanitize_name

__all__ = ["COVER_NAME", "chapter_filename", "chapter_number", "series_folder"]

COVER_NAME: Final = "cover.jpg"  # Mihon finds `cover.*` by name and reads the image by content
_MIN_DIGITS: Final = 4


def chapter_number(number: str | None) -> str | None:
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


def chapter_filename(number: str | None, title: str, fmt: str, *, volume: bool = False) -> str:
    """`Ch.0012 Dawn.cbz`: one chapter, one file (an EPUB is one chapter too). A volume that holds
    several chapters is `Vol.0003 Title.cbz` (Mihon reads `vol.` as the volume)."""
    extension = f".{fmt}"
    shown = chapter_number(number)
    label = "Vol" if volume else "Ch"
    prefix = f"{label}.{shown}" if shown is not None else ""
    room = MAX_COMPONENT_BYTES - len(prefix.encode()) - len(extension.encode()) - 1
    clean = sanitize_name(title, max_bytes=max(8, room)) if title.strip() else ""
    if clean == "_" and not title.strip(" ._"):
        clean = ""
    stem = " ".join(part for part in (prefix, clean) if part) or "Item"
    return f"{stem}{extension}"


def series_folder(name: str) -> str:
    """The folder of a series: one component, never hidden, never reserved."""
    return sanitize_name(name)
