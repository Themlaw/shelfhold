"""The Project Gutenberg source (EF-SRC-007, D6, ENF-LEG-003).

It reads the official catalogue (CSV or RDF, an address the admin gives) and takes the files from a
mirror the admin chose. It never asks for a page of the website: every request is made through the
Fetcher the host hands in, whose DomainRules name the two places the admin configured (the host
refuses any other name, and any path of the website outside the official feeds), and the plugin
itself refuses a mirror that is the website before it asks for anything.

The catalogue is read at most once a week (and revalidated with a conditional GET by the host's
cache), kept in memory, and never written anywhere. A book is a one-shot acquisition (D13): one
Series, one Unit, one EPUB. The files are untrusted: the host runs its neutralizer on every EPUB
from here, whatever this manifest says of its provenance.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import Callable
from typing import Any

from shelfhold.plugin_host.adapters.native.gutenberg.catalog import (
    CatalogBook,
    CatalogError,
    RdfReader,
    parse_catalog,
)
from shelfhold.plugin_host.adapters.native.gutenberg.mirror import (
    CATALOG_PATH_PREFIX,
    epub_url,
    setting_url,
)
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.models import (
    Cursor,
    DomainRule,
    FilterSpec,
    Page,
    Permissions,
    PluginKind,
    RateLimit,
)
from shelfhold.sdk.refs import FileAsset, ImageRequest, SeriesInfo, SeriesRef, UnitInfo
from shelfhold.sdk.source import (
    Capability,
    ContentRemoved,
    RateLimited,
    SourceChanged,
    SourceManifest,
    TemporarilyUnavailable,
)

__all__ = ["KEY", "PUBLISHER", "GutenbergSource", "build_manifest"]

KEY = "gutenberg"
PUBLISHER = "Project Gutenberg"
PAGE_SIZE = 25
REFRESH_SECONDS = 7 * 24 * 3600  # at most once a week (D6)
MIME_EPUB = "application/epub+zip"
# A placeholder that resolves nowhere (RFC 6761): until the admin gives an address, the rule names
# a host nothing can be sent to. The code carries no real host.
_UNSET = "unset.invalid"


def build_manifest() -> SourceManifest:
    """Same content as `sources/gutenberg/manifest.json` (a test keeps the two equal)."""
    return SourceManifest(
        key=KEY,
        kind=PluginKind.SOURCE,
        version="0.1.0",
        api=1,
        name="Project Gutenberg (official catalogue and a mirror)",
        permissions=Permissions(
            domains=[
                DomainRule(
                    pattern=f"catalog.{_UNSET}",
                    purpose="api",
                    base_url_setting="catalog_url",
                    path_prefix=CATALOG_PATH_PREFIX,
                ),
                DomainRule(
                    pattern=f"mirror.{_UNSET}", purpose="other", base_url_setting="mirror_url"
                ),
            ]
        ),
        settings_schema={
            "type": "object",
            "required": ["catalog_url", "mirror_url"],
            "properties": {
                "catalog_url": {
                    "type": "string",
                    "format": "uri",
                    "title": "Address of the official catalogue file (CSV or RDF)",
                },
                "mirror_url": {
                    "type": "string",
                    "format": "uri",
                    "title": "Address of a mirror from the official list",
                },
            },
        },
        content_warning="safe",
        terms_url="https://www.gutenberg.org/policy/terms_of_use.html",
        respects_robots=True,
        attribution=(
            "Project Gutenberg texts, in the public domain in the United States "
            "(not necessarily elsewhere)."
        ),
        langs=[],
        content_kinds={"file"},
        url_patterns=[],
        capabilities=Capability.SEARCH,
        rate_limit=RateLimit(requests_per_second=0.2, concurrency=1),
        rights_basis="us_public_domain",
        rights_jurisdiction="US",
        translation_nature="published",
        content_trust="untrusted",
    )


class GutenbergSource:
    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, rdf: RdfReader | None = None
    ) -> None:
        self.manifest = build_manifest()
        self._rdf = rdf  # the host's XML reader; without one only the CSV catalogue is read
        self._clock = clock
        self._books: dict[int, CatalogBook] | None = None
        self._loaded_at = 0.0
        self._loaded_from = ""
        self._locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()

    def filters(self) -> list[FilterSpec]:
        return [FilterSpec(key="language", kind="text", label="Language (code)")]

    # -- the catalogue ------------------------------------------------------------------------

    def _addresses(self, f: Fetcher) -> tuple[str, str]:
        settings = f.settings()
        return setting_url(settings, "catalog_url"), setting_url(settings, "mirror_url")

    def _fresh(self, catalog_url: str) -> bool:
        return (
            self._books is not None
            and self._loaded_from == catalog_url
            and self._clock() - self._loaded_at < REFRESH_SECONDS
        )

    async def _catalog(self, f: Fetcher, catalog_url: str) -> dict[int, CatalogBook]:
        loop = asyncio.get_running_loop()
        lock = self._locks.setdefault(loop, asyncio.Lock())
        async with lock:  # two operations at once read the catalogue once
            if self._books is not None and self._fresh(catalog_url):
                return self._books
            answer = await f.get(catalog_url)
            if answer.status == 429:
                raise RateLimited("the catalogue answered 429")
            if answer.status != 200:
                if self._books is not None and self._loaded_from == catalog_url:
                    return self._books  # keep what is known; the next call asks again
                raise TemporarilyUnavailable(f"the catalogue answered {answer.status}")
            try:
                books = parse_catalog(answer.body, rdf=self._rdf)
            except CatalogError as error:
                raise SourceChanged("the catalogue is not one this source reads") from error
            self._books = {b.pg_id: b for b in sorted(books, key=lambda b: b.pg_id)}
            self._loaded_at = self._clock()
            self._loaded_from = catalog_url
            return self._books

    async def _book(self, f: Fetcher, ref: SeriesRef) -> tuple[CatalogBook, str]:
        catalog_url, mirror = self._addresses(f)
        books = await self._catalog(f, catalog_url)
        digits = ref.remote_id
        book = books.get(int(digits)) if digits.isdigit() and len(digits) < 10 else None
        if book is None:
            raise ContentRemoved("no such book in the catalogue")
        return book, mirror

    # -- the contract -------------------------------------------------------------------------

    async def search(
        self,
        f: Fetcher,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        catalog_url, _mirror = self._addresses(f)
        books = await self._catalog(f, catalog_url)
        needle = " ".join(q.casefold().split())
        language = str(filters.get("language") or "").strip().casefold()
        found = [
            b
            for b in books.values()
            if needle in b.searchable and (not language or language in b.languages)
        ]
        offset = int(cursor.token) if cursor is not None and cursor.token.isdigit() else 0
        chunk = found[offset : offset + PAGE_SIZE]
        more = offset + PAGE_SIZE < len(found)
        return Page[SeriesRef](
            items=[SeriesRef(source_key=KEY, remote_id=str(b.pg_id), title=b.title) for b in chunk],
            next=Cursor(token=str(offset + PAGE_SIZE)) if more else None,
        )

    async def get_series(self, f: Fetcher, ref: SeriesRef) -> SeriesInfo:
        book, _mirror = await self._book(f, ref)
        names = [a.name for a in book.authors]
        return SeriesInfo(
            ref=ref,
            title=book.title,
            authors=names,
            translators=list(book.translators),
            publisher=book.publisher or PUBLISHER,
            external_ids={"gutenberg": str(book.pg_id)},
            author_death_years={a.name: a.death_year for a in book.authors if a.death_year},
            work_key=book.work_key,
        )

    async def list_units(self, f: Fetcher, ref: SeriesRef) -> list[UnitInfo]:
        book, _mirror = await self._book(f, ref)
        return [
            UnitInfo(
                remote_unit_id=f"{KEY}:{book.pg_id}:epub",
                source_order=1,
                kind="book",
                title=book.title,
                lang=book.languages[0] if len(book.languages) == 1 else None,  # type: ignore[arg-type]
                lang_raw=";".join(book.languages) or None,
                translation_nature="published",
            )
        ]

    async def fetch_unit(self, f: Fetcher, ref: SeriesRef, u: UnitInfo) -> FileAsset:
        book, mirror = await self._book(f, ref)
        if u.remote_unit_id != f"{KEY}:{book.pg_id}:epub":
            raise ContentRemoved("that Unit is not offered")
        return FileAsset(
            request=ImageRequest(url=epub_url(mirror, book.pg_id)),
            mime=MIME_EPUB,
            filename=f"pg{book.pg_id}.epub",
        )
