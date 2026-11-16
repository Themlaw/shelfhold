"""A reading list as a file says it (EF-BIB-016, M4 L18a): a name and books in order.

Pure data: what `adapters.cbl` reads out of a ComicRack-style list, and what the import turns into
the places of a collection. No identifier of the catalogue is in it, only what the file wrote.
"""

import unicodedata
from dataclasses import dataclass

__all__ = ["ReadingListBook", "ReadingListFile", "title_key"]


@dataclass(frozen=True, slots=True)
class ReadingListBook:
    series: str
    number: str = ""
    volume: str = ""
    year: str = ""


@dataclass(frozen=True, slots=True)
class ReadingListFile:
    name: str
    books: tuple[ReadingListBook, ...]


def title_key(title: str) -> str:
    """How two spellings of a title are told to be the same: NFKC, case and spaces folded."""
    return " ".join(unicodedata.normalize("NFKC", title).casefold().split())
