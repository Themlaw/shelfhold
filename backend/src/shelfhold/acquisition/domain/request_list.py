"""Reading a list of wanted books written as plain text (EF-USR-020).

One line is one title, and may carry an ISBN (`Title; 978-…`, `Title (978-…)`, a tab, or the ISBN
alone). The text comes from a file or a form a person pasted: it is untrusted, so it is bounded
before it is read (size, number of lines, length of a line), it is never interpreted (no markup,
no path, no URL is followed), and a control or format character is refused rather than stripped.
A list over a bound is refused whole, never cut: a half-read list would look like a complete one.
`#` starts a comment line; blank lines are skipped.

Pure: nothing here reads the database or the network.
"""

import re
import unicodedata
from dataclasses import dataclass

from shelfhold.acquisition.domain.request_target import InvalidTargetError, parse_target

__all__ = [
    "MAX_LINES",
    "MAX_LINE_CHARS",
    "MAX_LIST_BYTES",
    "ListLine",
    "ListRefusedError",
    "annotated_edition_asked",
    "parse_list",
]

MAX_LIST_BYTES = 64 * 1024
MAX_LINES = 100  # wanted books, not counting comments and blank lines
MAX_LINE_CHARS = 300
_FORBIDDEN = {"Cc", "Cf", "Cs", "Co", "Cn"}
# an ISBN-10 or ISBN-13 with the spaces or hyphens people write; checked by `parse_target` after
_ISBN_SHAPE = re.compile(r"(?<![\w-])(?:97[89][\s-]?)?\d(?:[\s-]?\d){8}[\s-]?[\dXx](?![\w-])")
_BEFORE = " \t;,|:-\u2013\u2014(["  # what may link a title to the ISBN written after it
_AFTER = " \t;,|:-\u2013\u2014)]"  # ... and before it
# What asks for one precise annotated edition (not just a book): a word of the "annotated",
# "critical", "commented" family, in French or English, on a folded title (no accent, lowercase).
_ANNOTATED = re.compile(
    r"\b(annot\w*|edition critique|critical edition|commente\w*|commented|variorum|"
    r"scholarly edition|edition savante|avec notes|with notes)\b"
)


class ListRefusedError(ValueError):
    """The list is not read; `reason` is a code, never a piece of the text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ListLine:
    position: int  # the line of the text among the wanted books, from 0
    text: str  # the title as written, without the ISBN (may be empty: an ISBN alone)
    isbn: str | None  # a valid ISBN-13, normalised
    annotated: bool  # the line asks for an annotated or critical edition


def _plain(text: str) -> str:
    """Lowercase, without accents: the form the words of `_ANNOTATED` are written in."""
    decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text).casefold())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def annotated_edition_asked(text: str) -> bool:
    return _ANNOTATED.search(_plain(text)) is not None


def parse_list(raw: str | bytes) -> list[ListLine]:
    """The wanted books of a text, in order. `ListRefusedError` for a text over a bound, one that is
    not text, or one that holds a line no book can be read from."""
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if len(data) > MAX_LIST_BYTES:
        raise ListRefusedError("too_large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ListRefusedError("not_text") from None
    lines: list[ListLine] = []
    for source in text.splitlines():
        stripped = source.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(stripped) > MAX_LINE_CHARS:
            raise ListRefusedError("line_too_long")
        if any(unicodedata.category(c) in _FORBIDDEN and c != "\t" for c in stripped):
            raise ListRefusedError("not_text")
        if len(lines) >= MAX_LINES:
            raise ListRefusedError("too_many_lines")
        lines.append(_line(len(lines), stripped))
    if not lines:
        raise ListRefusedError("empty")
    return lines


def _line(position: int, text: str) -> ListLine:
    isbn: str | None = None
    title = text
    for found in _ISBN_SHAPE.finditer(text):
        try:
            isbn = parse_target("isbn", found.group(0)).value
        except InvalidTargetError:
            continue  # digits that are not an ISBN stay part of the title (a year, a number)
        # only the separators that held the ISBN to the title go with it: `(`, `;`, a tab, a dash
        before = text[: found.start()].rstrip(_BEFORE)
        after = text[found.end() :].lstrip(_AFTER)
        title = f"{before} {after}"
        break
    title = " ".join(unicodedata.normalize("NFKC", title).split())
    return ListLine(position, title, isbn, annotated_edition_asked(text))
