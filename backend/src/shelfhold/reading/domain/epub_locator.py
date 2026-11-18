"""The xpointer of an EPUB, read with bounds (M5 L09, EF-LEC-009, EF-LEC-014, ADR-0053 section 6).

KOReader names a place in a reflowable book by an xpointer: `/body/DocFragment[3]/body/div[2]/p[12]/
text().5`. `DocFragment[n]` is the n-th document of the spine, counted from 1. That is all Shelfhold
takes from it, and it takes it by **parsing**, never by running: the text is bounded in length, in
number of steps and in the length of each name, matched part by part against a fixed grammar (no
pattern runs over the whole text, so none backtracks), and anything that does not fit gives `None`.
The caller keeps the text as the device wrote it (an opaque locator, given back and relayed as it
came) and draws nothing from it.

What the index is for: a Unit of an EPUB is read only when the **last document of the spine** is
reached at its end (`reaches_end`), by a plausible walk of one device
(`reading.application.progress`).
A place in the middle of a book, a leap, or a spine nobody could tell is a place and no more.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "END_SHARE",
    "MAX_FRAGMENT",
    "MAX_NAME",
    "MAX_STEPS",
    "MAX_XPOINTER",
    "Xpointer",
    "parse_xpointer",
    "reaches_end",
    "spine_index",
]

MAX_XPOINTER: Final = 2048  # the whole text (the bound of the locator `kosync` stores)
MAX_STEPS: Final = 64  # elements below the document, `body` included
MAX_NAME: Final = 64  # one element name
MAX_FRAGMENT: Final = 100_000  # no book has more documents
MAX_INDEX: Final = 100_000  # `p[12]`
MAX_OFFSET: Final = 10**9  # `.5`
END_SHARE: Final = 0.99  # the share from which the last document is « at its end »

_NUMBER = re.compile(r"[0-9]{1,9}")  # ASCII digits only: `\d` would take other scripts too
_FRAGMENT = re.compile(r"DocFragment\[([0-9]{1,6})\]")
_STEP = re.compile(r"([A-Za-z][A-Za-z0-9_:-]*)(?:\[([0-9]{1,6})\])?")


@dataclass(frozen=True)
class Xpointer:
    fragment: int  # the document of the spine, from 1
    steps: int  # elements below the document
    offset: int | None  # the character in the text, when the xpointer names one

    @property
    def spine_index(self) -> int:
        return self.fragment - 1


def parse_xpointer(text: str) -> Xpointer | None:
    """The document an xpointer is in, or `None` when it does not fit the grammar or a bound."""
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_XPOINTER or not text.isascii():
        return None
    parts = text.split("/")
    if len(parts) < 3 or parts[0] != "" or parts[1] != "body":
        return None
    fragment = _FRAGMENT.fullmatch(parts[2])
    if fragment is None:
        return None
    number = int(fragment.group(1))
    if not 1 <= number <= MAX_FRAGMENT:
        return None
    rest = parts[3:]
    if len(rest) > MAX_STEPS:
        return None
    offset: int | None = None
    for position, part in enumerate(rest):
        last = position == len(rest) - 1
        if last and part.startswith("text()"):
            tail = part[len("text()") :]
            node = re.match(r"\[([0-9]{1,6})\]", tail)  # `text()[2].15`: the second text node
            if node is not None:
                tail = tail[node.end() :]
            if tail:
                offset = _offset(tail)
                if offset is None:
                    return None
            continue
        name, dot, after = part.partition(".")
        if dot:  # `p.12`: the character, on the last element
            if not last:
                return None
            offset = _offset(dot + after)
            if offset is None:
                return None
        step = _STEP.fullmatch(name)
        if step is None or len(step.group(1)) > MAX_NAME:
            return None
        if step.group(2) is not None and int(step.group(2)) > MAX_INDEX:
            return None
    return Xpointer(number, len(rest), offset)


def _offset(tail: str) -> int | None:
    """`.12`: a character count, from 0."""
    if not tail.startswith(".") or _NUMBER.fullmatch(tail[1:]) is None:
        return None
    value = int(tail[1:])
    return value if value <= MAX_OFFSET else None


def spine_index(text: str, spine_len: int | None = None) -> int | None:
    """The 0-based index of the document the xpointer is in; with `spine_len`, `None` for a
    document the spine does not have."""
    parsed = parse_xpointer(text)
    if parsed is None:
        return None
    if spine_len is not None and not 1 <= parsed.fragment <= spine_len:
        return None
    return parsed.spine_index


def reaches_end(text: str, share: float, spine_len: int | None) -> bool:
    """The last document of the spine, and the end of the book by the device's own share. A spine
    nobody could tell (`None`, or no document) is no end."""
    if spine_len is None or spine_len < 1:
        return False
    if isinstance(share, bool) or not isinstance(share, int | float) or not math.isfinite(share):
        return False
    if not END_SHARE <= share <= 1.0:
        return False
    parsed = parse_xpointer(text)
    return parsed is not None and parsed.fragment == spine_len
