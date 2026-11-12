"""The official Project Gutenberg catalogue, as CSV or RDF/XML (EF-SRC-007).

A catalogue is data from the network. It is read as text: nothing in it is executed, resolved or
followed. A row that is not a book (an audio recording, a bad number, no title) is skipped, never
fatal; a document with too many books, or none of the expected shape, is refused whole.

The CSV is read here. An RDF document is XML, and the host has one entry point for XML
(`safe_parse_xml`, ENF-SEC-014) that a native adapter may not import: the host hands the source
its reader (`RdfReader`, `shelfhold.plugin_host.gutenberg_rdf`), and without one a document that is
RDF is refused.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = [
    "MAX_BOOKS",
    "MAX_PG_ID",
    "Author",
    "CatalogBook",
    "CatalogError",
    "RdfReader",
    "clean_text",
    "make_book",
    "parse_catalog",
    "parse_csv",
    "work_key_of",
]

MAX_BOOKS = 400_000  # the whole catalogue is about 80 000 books; a document far over is not it
MAX_PG_ID = 10**9
MAX_SUBJECTS = 50  # subjects kept per book
_HEADER = ("Text#", "Type", "Title", "Language", "Authors")
_YEARS = re.compile(r",\s*(\d{1,4})?\s*-\s*(\d{1,4})?\s*$")
_SPACE = re.compile(r"\s+")


class CatalogError(ValueError):
    """The document is not a catalogue this source reads."""


@dataclass(frozen=True)
class Author:
    name: str  # "Austen, Jane": as the catalogue writes it
    death_year: int | None = None


@dataclass(frozen=True)
class CatalogBook:
    pg_id: int
    title: str
    languages: tuple[str, ...]
    authors: tuple[Author, ...] = ()
    translators: tuple[str, ...] = ()
    publisher: str | None = None
    issued: str | None = None
    searchable: str = field(default="", compare=False)
    subjects: tuple[str, ...] = field(default=(), compare=False)  # the catalogue's "Subjects"

    @property
    def work_key(self) -> str:
        return work_key_of(self.title, self.authors[0].name if self.authors else "")


def work_key_of(title: str, first_author: str) -> str:
    """What two books of the same Work have in common in this catalogue: the title and the first
    author, without case, spacing or punctuation."""
    plain = re.sub(r"[^\w]+", " ", f"{title}|{first_author}".casefold())
    return _SPACE.sub(" ", plain).strip()


def clean_text(text: str | None) -> str:
    return _SPACE.sub(" ", (text or "").replace("\x00", "")).strip()


def make_book(
    pg_id: int,
    title: str,
    languages: tuple[str, ...],
    authors: tuple[Author, ...],
    translators: tuple[str, ...] = (),
    publisher: str | None = None,
    issued: str | None = None,
    subjects: tuple[str, ...] = (),
) -> CatalogBook:
    searchable = " ".join([title, *(a.name for a in authors)]).casefold()
    return CatalogBook(
        pg_id, title, languages, authors, translators, publisher, issued, searchable, subjects
    )


def _author_of(raw: str) -> Author | None:
    text = clean_text(raw)
    if not text:
        return None
    years = _YEARS.search(text)
    if years is None:
        return Author(text)
    name = clean_text(text[: years.start()])
    death = years.group(2)
    return Author(name, int(death) if death else None) if name else None


def parse_csv(raw: bytes, *, limit: int = MAX_BOOKS) -> list[CatalogBook]:
    """`pg_catalog.csv`: `Text#,Type,Issued,Title,Language,Authors,...`; only `Text` rows."""
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None or not set(_HEADER) <= set(reader.fieldnames):
        raise CatalogError("not the Project Gutenberg CSV catalogue")
    books: list[CatalogBook] = []
    for row in reader:
        if clean_text(row.get("Type")).casefold() != "text":
            continue
        number = clean_text(row.get("Text#"))
        title = clean_text(row.get("Title"))
        if not number.isdigit() or not title or not 0 < int(number) < MAX_PG_ID:
            continue
        languages = tuple(
            x for x in (clean_text(p) for p in (row.get("Language") or "").split(";")) if x
        )
        authors = tuple(
            a for a in (_author_of(p) for p in (row.get("Authors") or "").split(";")) if a
        )
        subjects = tuple(
            x for x in (clean_text(p) for p in (row.get("Subjects") or "").split(";")) if x
        )[:MAX_SUBJECTS]
        books.append(
            make_book(
                int(number),
                title,
                languages,
                authors,
                issued=clean_text(row.get("Issued")) or None,
                subjects=subjects,
            )
        )
        if len(books) > limit:
            raise CatalogError("the catalogue holds more books than a catalogue can")
    return books


RdfReader = Callable[[bytes, int], list[CatalogBook]]  # (document, limit) -> books


def parse_catalog(
    raw: bytes, *, rdf: RdfReader | None = None, limit: int = MAX_BOOKS
) -> list[CatalogBook]:
    """CSV or RDF, told from the first byte that is not a space."""
    head = raw.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    if not head:
        raise CatalogError("an empty catalogue")
    if head == b"<":
        if rdf is None:
            raise CatalogError("an RDF catalogue needs the host's XML reader")
        return rdf(raw, limit)
    return parse_csv(raw, limit=limit)
