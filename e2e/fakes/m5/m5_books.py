"""Synthetic EPUB files of the M5 journeys: a book of several chapters (so that an excerpt can be
cut from it) and a booby-trapped one. Made here from nothing, public-domain-style filler text, no
title of any real catalogue. The container comes from the corpus builder of the SDK."""

from __future__ import annotations

import hashlib

from shelfhold.sdk.testing.corpus.trapped_epubs.build import (
    EPUB_NS,
    NAV,
    STYLE,
    TINY_PNG,
    XHTML_NS,
    Member,
    zip_bytes,
)
from shelfhold.sdk.testing.corpus.trapped_epubs.build import CONTAINER

__all__ = ["BEACON", "chapter_text", "multi_chapter_epub"]

BEACON = "/__e2e_beacon"  # the address a script of a trapped book would call back, if it ran


def chapter_text(title: str, number: int) -> str:
    """What chapter `number` of the book says (a marker the journeys look for on the page)."""
    return f"Marqueur {title} chapitre {number}"


def _chapter(title: str, number: int, trap: bool, filler: int) -> bytes:
    body = f"<h1>Chapitre {number}</h1><p>{chapter_text(title, number)}</p>"
    body += "<p>" + ("Lorem synthétique, texte de remplissage. " * filler) + "</p>"
    if trap and number == 1:
        body += (
            f'<script>fetch("{BEACON}?script");document.title="pwned"</script>'
            f'<img src="x" onerror="fetch(\'{BEACON}?onerror\')" alt=""/>'
            f'<a href="javascript:fetch(\'{BEACON}?href\')">lien piégé</a>'
            f'<p onclick="fetch(\'{BEACON}?click\')">Paragraphe piégé</p>'
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<html xmlns="{XHTML_NS}" xmlns:epub="{EPUB_NS}"><head><title>T</title></head>'
        f"<body>{body}</body></html>"
    ).encode()


def multi_chapter_epub(
    title: str, *, chapters: int = 10, trap: bool = False, author: str = "Autrice Inventée",
    filler: int = 40,
) -> bytes:  # fmt: skip
    """A well-formed EPUB of `chapters` chapters of the same size, one spine entry each; an
    excerpt (a fifth of the whole, ADR-0055) is then its first two. `trap`: the first chapter
    carries every way a script gets run (a `script` element, an event attribute, an SVG `onload`,
    a `javascript:` link): each would call `BEACON` if it ran."""
    ident = hashlib.sha256(title.encode()).hexdigest()[:32]
    manifest = "".join(
        f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(1, chapters + 1)
    )
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, chapters + 1))
    package = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="id">urn:uuid:{ident}</dc:identifier>'
        f"<dc:title>{title}</dc:title><dc:language>fr</dc:language>"
        f"<dc:creator>{author}</dc:creator></metadata><manifest>"
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        f'<item id="css" href="style.css" media-type="text/css"/>{manifest}</manifest>'
        f"<spine>{spine}</spine></package>"
    ).encode()
    return zip_bytes([
        Member("mimetype", b"application/epub+zip", deflate=False),
        Member("META-INF/container.xml", CONTAINER),
        Member("OEBPS/content.opf", package),
        Member("OEBPS/nav.xhtml", NAV),
        Member("OEBPS/style.css", STYLE),
        Member("OEBPS/cover.png", TINY_PNG, deflate=False),
        *(
            Member(f"OEBPS/ch{i}.xhtml", _chapter(title, i, trap, filler))
            for i in range(1, chapters + 1)
        ),
    ])  # fmt: skip
