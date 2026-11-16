"""The reading-list file (CBL, EF-BIB-016, M4 L18a): untrusted XML, read bounded, without entities.

A `.cbl` comes from a person, a forum or a shared folder: it is read as hostile. The bytes go
through `safe_parse_xml` (the one XML door, ENF-SEC-014), which refuses every DTD, so no external
entity (file or network), no parameter entity and no expansion can be reached; on top of it the
size, the nesting, the number of elements, the number of books and the length of each text are
capped, so the work stays proportional to the cap and not to what the file claims. A file past a
cap is refused whole, never cut: a list read to its first thousand books would look complete.

Only the order and the words of the file come out: `Name`, and for each `Book` its `Series`,
`Number`, `Volume` and `Year`. Everything else (matchers, databases, a `Source` of a tool) is
ignored. Which Work a line stands for is the import's business (`application.collections`).
"""

from typing import Final

from shelfhold.catalog.domain.reading_list import ReadingListBook, ReadingListFile
from shelfhold.infrastructure.xml import UnsafeXmlError, XmlLimits, safe_parse_xml

__all__ = ["MAX_BOOKS", "MAX_CBL_BYTES", "CblError", "parse_cbl"]

MAX_CBL_BYTES: Final = 2 * 1024 * 1024
MAX_BOOKS: Final = 5000
MAX_DEPTH: Final = 8
MAX_TEXT: Final = 300
MAX_NAME: Final = 200  # `collection.name`
_LIMITS: Final = XmlLimits(
    max_bytes=MAX_CBL_BYTES, max_depth=MAX_DEPTH, max_elements=MAX_BOOKS + 64, max_attributes=32
)


class CblError(ValueError):
    """The file is refused. `reason` is a stable word, never a piece of the file."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _text(raw: str | None, *, limit: int) -> str:
    value = " ".join((raw or "").split())
    if len(value) > limit:
        raise CblError("too_long")
    return value


def parse_cbl(data: bytes) -> ReadingListFile:
    """The name and the books, in the order of the file; or `CblError`."""
    try:
        root = safe_parse_xml(data, limits=_LIMITS)
    except UnsafeXmlError as error:
        raise CblError(error.reason) from None
    if root.tag != "ReadingList":
        raise CblError("not_a_reading_list")
    books_node = root.find("Books")
    if books_node is None:
        raise CblError("not_a_reading_list")
    name = _text(root.findtext("Name"), limit=MAX_NAME)
    books: list[ReadingListBook] = []
    for node in books_node.findall("Book"):
        if len(books) >= MAX_BOOKS:
            raise CblError("too_many_books")
        series = _text(node.get("Series"), limit=MAX_TEXT)
        if not series:
            raise CblError("malformed")
        books.append(
            ReadingListBook(
                series=series,
                number=_text(node.get("Number"), limit=40),
                volume=_text(node.get("Volume"), limit=40),
                year=_text(node.get("Year"), limit=40),
            )
        )
    return ReadingListFile(name=name, books=tuple(books))
