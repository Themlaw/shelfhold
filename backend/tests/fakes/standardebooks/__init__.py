"""Synthetic Standard Ebooks documents (M2 L24): the paths of a book and its downloads, and the
OPDS feeds the patron feed serves, built with the OPDS fakes. Nothing here is copied from the real
site: the authors and titles are made up.
"""

from __future__ import annotations

from tests.fakes.opds import (
    ATOM,
    OPENSEARCH,
    acquisition_entry,
    atom_feed,
    kavita_description,
)
from tests.fakes.opds.feeds import link

__all__ = ["FEED_ROOT", "book_path", "download_path", "patron_results", "patron_root", "search_doc"]

FEED_ROOT = "/feeds/opds"


def book_path(slug: str) -> str:
    return f"/ebooks/{slug}"


def download_path(slug: str) -> str:
    return f"/ebooks/{slug}/downloads/{slug.replace('/', '_')}.epub"


def patron_root() -> bytes:
    return atom_feed(
        "urn:test:se:root",
        "Catalogue de test",
        [],
        links=[link("search", f"{FEED_ROOT}/search", OPENSEARCH)],
    )


def search_doc() -> bytes:
    return kavita_description(f"{FEED_ROOT}/all?query={{searchTerms}}")


def patron_results(slugs: list[str], *, next_href: str | None = None) -> bytes:
    entries = [
        acquisition_entry(
            f"urn:test:se:{slug}",
            slug.rsplit("/", 1)[-1].replace("-", " ").title(),
            download_path(slug),
            "application/epub+zip",
            author="Autrice Inventée",
            lang="en",
        )
        for slug in slugs
    ]
    links = [link("next", next_href, ATOM)] if next_href else []
    return atom_feed("urn:test:se:all", "Résultats", entries, links=links)
