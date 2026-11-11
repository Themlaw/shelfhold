"""Builders of OPDS 1.2 (Atom) and OPDS 2.0 (JSON) documents, and of OpenSearch descriptions."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from xml.sax.saxutils import escape, quoteattr

ATOM = "application/atom+xml;profile=opds-catalog;kind=acquisition"
NAVIGATION = "application/atom+xml;profile=opds-catalog;kind=navigation"
ENTRY = "application/atom+xml;type=entry;profile=opds-catalog"
OPENSEARCH = "application/opensearchdescription+xml"
OPDS2 = "application/opds+json"
ACQUISITION = "http://opds-spec.org/acquisition"
OPEN_ACCESS = "http://opds-spec.org/acquisition/open-access"
BUY = "http://opds-spec.org/acquisition/buy"
EPUB = "application/epub+zip"
CBZ = "application/vnd.comicbook+zip"
HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog" '
    'xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">\n'
)


def link(rel: str, href: str, mime: str | None = None, title: str | None = None) -> str:
    parts = [f"rel={quoteattr(rel)}", f"href={quoteattr(href)}"]
    if mime:
        parts.append(f"type={quoteattr(mime)}")
    if title:
        parts.append(f"title={quoteattr(title)}")
    return "<link " + " ".join(parts) + "/>"


def acquisition_entry(
    entry_id: str,
    title: str,
    href: str,
    mime: str = EPUB,
    *,
    author: str = "Autrice Inventée",
    summary: str = "Un résumé inventé.",
    rel: str = ACQUISITION,
    extra: Iterable[str] = (),
    lang: str = "fr",
) -> str:
    links = "\n    ".join([link(rel, href, mime), *extra])
    return (
        f"  <entry>\n    <id>{escape(entry_id)}</id>\n    <title>{escape(title)}</title>\n"
        f"    <updated>2026-01-01T00:00:00Z</updated>\n"
        f"    <author><name>{escape(author)}</name></author>\n"
        f"    <dcterms:language>{lang}</dcterms:language>\n"
        f"    <summary>{escape(summary)}</summary>\n    {links}\n  </entry>\n"
    )


def navigation_entry(entry_id: str, title: str, href: str, *, rel: str = "subsection") -> str:
    return (
        f"  <entry>\n    <id>{escape(entry_id)}</id>\n    <title>{escape(title)}</title>\n"
        f"    <updated>2026-01-01T00:00:00Z</updated>\n    {link(rel, href, ATOM)}\n  </entry>\n"
    )


def atom_feed(
    feed_id: str,
    title: str,
    entries: Iterable[str] = (),
    *,
    links: Iterable[str] = (),
) -> bytes:
    body = "".join(entries)
    head = "\n".join(f"  {x}" for x in links)
    return (
        f"{HEADER}  <id>{escape(feed_id)}</id>\n  <title>{escape(title)}</title>\n"
        f"  <updated>2026-01-01T00:00:00Z</updated>\n{head}\n{body}</feed>\n"
    ).encode()


def entry_document(entry: str) -> bytes:
    """An entry served alone (`type=entry`): the same element, with the namespaces."""
    inner = entry.replace("  <entry>", "<entry>", 1)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + inner.replace(
            "<entry>",
            '<entry xmlns="http://www.w3.org/2005/Atom" xmlns:dcterms="http://purl.org/dc/terms/">',
            1,
        )
    ).encode()


def kavita_description(template: str) -> bytes:
    """What Kavita serves at `/api/opds/<key>/search`: an OpenSearch description."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">\n'
        "  <ShortName>Catalogue de test</ShortName>\n"
        "  <Description>Recherche</Description>\n"
        f'  <Url type="{ATOM}" template={quoteattr(template)}/>\n'
        "</OpenSearchDescription>\n"
    ).encode()


def komga_description(template: str) -> bytes:
    """Komga also lists an HTML form first: the description holds two `Url`."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">\n'
        "  <ShortName>Bibliothèque de test</ShortName>\n"
        f'  <Url type="text/html" template={quoteattr("/search?q={searchTerms}")}/>\n'
        f'  <Url type="application/atom+xml" template={quoteattr(template)}/>\n'
        "</OpenSearchDescription>\n"
    ).encode()


def opds2_feed(
    title: str,
    *,
    publications: Iterable[dict[str, Any]] = (),
    navigation: Iterable[dict[str, Any]] = (),
    links: Iterable[dict[str, Any]] = (),
) -> bytes:
    doc: dict[str, Any] = {
        "metadata": {"title": title},
        "links": list(links),
    }
    if navigation:
        doc["navigation"] = list(navigation)
    if publications:
        doc["publications"] = list(publications)
    return json.dumps(doc).encode()


def publication(
    identifier: str, title: str, href: str, mime: str = EPUB, *, author: str = "Autrice Inventée"
) -> dict[str, Any]:
    return {
        "metadata": {
            "@type": "http://schema.org/Book",
            "identifier": identifier,
            "title": title,
            "author": [{"name": author}],
            "language": "fr",
            "description": "Un résumé inventé.",
        },
        "links": [
            {"rel": "self", "href": f"/opds/v2/publications/{identifier}", "type": OPDS2},
            {"rel": ACQUISITION, "href": href, "type": mime},
        ],
        "images": [],
    }
