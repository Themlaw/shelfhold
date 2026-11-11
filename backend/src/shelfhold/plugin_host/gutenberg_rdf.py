"""The RDF reader of the Gutenberg source (EF-SRC-007): the host's own, handed to the source.

An RDF catalogue is XML from the network. The host has one door for XML, `safe_parse_xml`
(ENF-SEC-014): no DTD, no entity, no external reference, and caps on size, depth and elements. A
native adapter may not import it, so the host builds the source with this reader.
"""

from __future__ import annotations

from shelfhold.infrastructure.xml import Element, UnsafeXmlError, XmlLimits, safe_parse_xml
from shelfhold.plugin_host.adapters.native.gutenberg.catalog import (
    MAX_PG_ID,
    Author,
    CatalogBook,
    CatalogError,
    clean_text,
    make_book,
)

__all__ = ["read_rdf"]

_LIMITS = XmlLimits(max_bytes=32 * 1024 * 1024, max_elements=2_000_000)
_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcterms": "http://purl.org/dc/terms/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
    "marcrel": "http://id.loc.gov/vocabulary/relators/",
}


def _text(node: Element | None, path: str) -> str:
    found = node.find(path, _NS) if node is not None else None
    return clean_text(found.text if found is not None else "")


def _agents(ebook: Element, tag: str) -> list[Element]:
    return [a for holder in ebook.findall(tag, _NS) for a in holder.findall("pgterms:agent", _NS)]


def read_rdf(raw: bytes, limit: int) -> list[CatalogBook]:
    """The `pgterms:ebook` elements of an RDF/XML catalogue (one or many in a document)."""
    try:
        root = safe_parse_xml(raw.lstrip(), limits=_LIMITS)
    except UnsafeXmlError as error:
        raise CatalogError(f"not a safe RDF document ({error.reason})") from error
    books: list[CatalogBook] = []
    for ebook in root.iter(f"{{{_NS['pgterms']}}}ebook"):
        number = ebook.get(f"{{{_NS['rdf']}}}about", "").rsplit("/", 1)[-1]
        title = _text(ebook, "dcterms:title")
        kind = _text(ebook, "dcterms:type/rdf:Description/rdf:value")
        if not number.isdigit() or not title or not 0 < int(number) < MAX_PG_ID:
            continue
        if kind and kind.casefold() != "text":
            continue
        languages = tuple(
            v
            for v in (
                clean_text(n.text)
                for n in ebook.findall("dcterms:language/rdf:Description/rdf:value", _NS)
            )
            if v
        )
        authors: list[Author] = []
        for agent in _agents(ebook, "dcterms:creator"):
            name = _text(agent, "pgterms:name")
            death = _text(agent, "pgterms:deathdate")
            if name:
                authors.append(Author(name, int(death) if death.isdigit() else None))
        translators = tuple(
            n for n in (_text(a, "pgterms:name") for a in _agents(ebook, "marcrel:trl")) if n
        )
        books.append(
            make_book(
                int(number), title, languages, tuple(authors), translators,
                _text(ebook, "dcterms:publisher") or None,
                _text(ebook, "dcterms:issued") or None,
            )
        )  # fmt: skip
        if len(books) > limit:
            raise CatalogError("the catalogue holds more books than a catalogue can")
    if not books and not root.tag.endswith("RDF"):
        raise CatalogError("not an RDF catalogue")
    return books
