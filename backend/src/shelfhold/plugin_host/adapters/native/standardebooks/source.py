"""The Standard Ebooks source (EF-SRC-011, D3, ENF-LEG-003, ENF-LEG-008, EF-SRC-013).

Two ways in, and no third:

- **One address per book.** A book is `author/title` under `/ebooks/` of the site. Its title comes
  from that path, its file from the download of that very path: one request, nothing else. The
  source never reads a page of the site, a listing of authors or of subjects, and never walks
  anything (no harvesting: the site gives its full catalogue to its patrons, through the feed
  below).
- **The patron feed.** The OPDS feed of the site is reserved to the patrons of Standard Ebooks. It
  is read only when the admin chose the patron's credentials (`auth = basic`, the user name as a
  plain setting and the password as a sealed secret); without them no request goes to `/feeds/`,
  whatever is asked. The feed is read by the generic OPDS source (L22), which is reused, not
  copied; here it is held to one page of results per read (a cursor is handed back, never
  followed) and to a book of this site (`/ebooks/.../downloads/*.epub`).

The credential never reaches this code: the host injects it into the Fetcher, only towards the
origin the manifest pins (`base_url` has one value), whatever a setting or a feed says. The files
are `untrusted`: the host neutralizes every EPUB, as for every native source (L23). Standard
Ebooks and the texts it republishes are free of rights in the United States (ENF-LEG-008), which
is not necessarily so in the country of the instance.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Final
from urllib.parse import urlsplit

from shelfhold.plugin_host.adapters.native.opds import OpdsSource
from shelfhold.plugin_host.adapters.native.opds.atom import ParseXml
from shelfhold.plugin_host.adapters.native.opds.model import Feed
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.lang import LanguageTag
from shelfhold.sdk.models import (
    AuthBasic,
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
    AuthRequired,
    Capability,
    ContentRemoved,
    SourceManifest,
)

__all__ = ["KEY", "ORIGIN", "StandardEbooksSource", "build_manifest", "parse_slug"]

KEY: Final = "standardebooks"
PUBLISHER: Final = "Standard Ebooks"
ORIGIN: Final = "https://standardebooks.org"
FEED_ROOT: Final = f"{ORIGIN}/feeds/opds"
MIME_EPUB: Final = "application/epub+zip"
CC0: Final = "CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/)"
MAX_SLUG: Final = 200
LANG: Final = LanguageTag("en")

# `author/title`, and for a translated or illustrated edition one more segment (the person).
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*){1,2}")
# Third segments that are parts of the site, not people.
_PARTS: Final = frozenset({"downloads", "text", "dist", "src", "feeds"})
_DOWNLOAD = re.compile(r"/ebooks/(?P<slug>[^/]+/[^/]+(?:/[^/]+)?)/downloads/[^/]+\.epub")


def build_manifest() -> SourceManifest:
    """Same content as `sources/standardebooks/manifest.json` (a test keeps the two equal)."""
    return SourceManifest(
        key=KEY,
        kind=PluginKind.SOURCE,
        version="0.1.0",
        api=1,
        name="Standard Ebooks (one address per book, patron feed with credentials)",
        permissions=Permissions(
            domains=[
                DomainRule(pattern="standardebooks.org", purpose="other", path_prefix="/ebooks/"),
                DomainRule(pattern="standardebooks.org", purpose="api", path_prefix="/feeds/"),
            ]
        ),
        settings_schema={
            "type": "object",
            "properties": {
                # one value: the credential goes to this origin, whatever a setting says
                "base_url": {
                    "type": "string",
                    "title": "Origin of the patron credential (fixed)",
                    "enum": [ORIGIN],
                    "default": ORIGIN,
                },
                "auth": {
                    "type": "string",
                    "title": "Patron feed: none, or basic with the patron's credentials",
                    "enum": ["none", "basic"],
                    "default": "none",
                },
                "username": {"type": "string", "title": "Patron user name (basic)"},
                "password": {"type": "string", "format": "password", "title": "Patron password"},
            },
        },
        content_warning="safe",
        terms_url="https://standardebooks.org/about",
        respects_robots=True,
        attribution=(
            "Standard Ebooks productions, dedicated to the public domain (CC0); the texts are "
            "free of rights in the United States, not necessarily elsewhere."
        ),
        langs=[LANG],
        content_kinds={"file"},
        url_patterns=[f"{ORIGIN}/ebooks/*/*", f"{ORIGIN}/ebooks/*/*/*"],
        capabilities=Capability.SEARCH | Capability.URL_RESOLVE,
        rate_limit=RateLimit(requests_per_second=0.2, concurrency=1),
        auth=[AuthBasic()],
        rights_basis="us_public_domain",
        rights_jurisdiction="US",
        translation_nature="unknown",
        content_trust="untrusted",
    )


def parse_slug(text: str) -> str:
    """`author/title` of a book path, or a refusal. Nothing that could leave `/ebooks/`."""
    if not text or len(text) > MAX_SLUG or _SLUG.fullmatch(text) is None:
        raise ContentRemoved("not the path of a book")
    parts = text.split("/")
    if len(parts) == 3 and parts[2] in _PARTS:
        raise ContentRemoved("not the path of a book")
    return text


def _download_slug(url: str) -> str:
    """The book a download address belongs to: a file of this site's `/ebooks/` and nothing else."""
    parts = urlsplit(url)
    found = _DOWNLOAD.fullmatch(parts.path)
    if f"{parts.scheme}://{parts.netloc}" != ORIGIN or found is None:
        raise ContentRemoved("not a download of this site")
    return parse_slug(found["slug"])


def _page(slug: str) -> str:
    return f"{ORIGIN}/ebooks/{slug}"


def _file(slug: str) -> str:
    return f"{_page(slug)}/downloads/{slug.replace('/', '_')}.epub"


def _title(slug: str) -> str:
    return slug.split("/")[1].replace("-", " ").title()


def _rights(slug: str) -> str:
    """The origin link first (the host takes the first link as `dc:source`), then the licence."""
    return (
        f"Source: {_page(slug)}. Licence of the production: {CC0}. The text is free of rights "
        f"in the United States. Producer: {PUBLISHER}."
    )


class _OnePage(OpdsSource):
    """The generic OPDS source, held to one page per read: a search or a series reads the page it
    was asked for and hands back what follows, it never follows it (no harvesting)."""

    async def _pages(self, f: Fetcher, url: str) -> list[Feed]:
        return [await self._read(f, self._inside(url))]


class StandardEbooksSource:
    def __init__(self, parse_xml: ParseXml, *, patron: bool = False) -> None:
        self.manifest = build_manifest()
        self._parse_xml: Callable[..., Any] = parse_xml
        self._patron = patron

    def configured(self, settings: Mapping[str, Any]) -> StandardEbooksSource:
        """A copy that knows whether the admin chose the patron's credentials (a plain value: the
        credential itself is never given to the source, the host injects it)."""
        return StandardEbooksSource(self._parse_xml, patron=settings.get("auth") == "basic")

    def filters(self) -> list[FilterSpec]:
        return []

    # -- the patron feed (the generic OPDS source, with the host's credential) ---------------------

    def _feed(self) -> OpdsSource:
        """Only for a source whose admin chose the patron's credentials: otherwise a refusal
        before any request."""
        if not self._patron:
            raise AuthRequired("the patron feed needs the patron's credentials")
        return _OnePage(self._parse_xml, base_url=FEED_ROOT)

    @staticmethod
    def _is_feed_ref(ref: SeriesRef) -> bool:
        return ref.remote_id.startswith("/")

    async def search(
        self,
        f: Fetcher,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        # the feed's categories travel in the memo as `"subjects"` (the family filter reads them)
        page = await self._feed().search(f, q, filters, cursor, sub=sub)
        return Page[SeriesRef](
            items=[r.model_copy(update={"source_key": KEY}) for r in page.items], next=page.next
        )

    # -- one address per book ---------------------------------------------------------------------

    async def resolve_url(self, f: Fetcher, url: str) -> SeriesRef | UnitInfo | None:
        """The book a pasted address names, without asking anything."""
        try:
            parts = urlsplit(url)
            if f"{parts.scheme.lower()}://{parts.netloc.lower()}" != ORIGIN:
                return None
            slug = parse_slug(parts.path.removeprefix("/ebooks/"))
        except (ContentRemoved, ValueError):
            return None
        if not parts.path.startswith("/ebooks/"):
            return None
        return SeriesRef(source_key=KEY, remote_id=slug, title=_title(slug))

    async def _slug_of_feed_book(self, f: Fetcher, ref: SeriesRef) -> tuple[OpdsSource, str]:
        feed = self._feed()
        units = await feed.list_units(f, ref)
        if not units:
            raise ContentRemoved("the feed offers no book there")
        asset = await feed.fetch_unit(f, ref, units[0])
        return feed, _download_slug(asset.request.url)

    async def get_series(self, f: Fetcher, ref: SeriesRef) -> SeriesInfo:
        slug = (
            (await self._slug_of_feed_book(f, ref))[1]
            if self._is_feed_ref(ref)
            else parse_slug(ref.remote_id)
        )
        return SeriesInfo(
            ref=ref,
            title=_title(slug),
            publisher=PUBLISHER,
            external_ids={"standardebooks": slug},
            rights=_rights(slug),
        )

    async def list_units(self, f: Fetcher, ref: SeriesRef) -> list[UnitInfo]:
        if self._is_feed_ref(ref):
            slug = (await self._slug_of_feed_book(f, ref))[1]
        else:
            slug = parse_slug(ref.remote_id)
        return [
            UnitInfo(
                remote_unit_id=f"{KEY}:{slug}:epub",
                source_order=1,
                kind="book",
                title=_title(slug),
                lang=LANG,
                lang_raw="en",
            )
        ]

    async def fetch_unit(self, f: Fetcher, ref: SeriesRef, u: UnitInfo) -> FileAsset:
        if self._is_feed_ref(ref):
            slug = (await self._slug_of_feed_book(f, ref))[1]
        else:
            slug = parse_slug(ref.remote_id)
        if u.remote_unit_id != f"{KEY}:{slug}:epub":
            raise ContentRemoved("that Unit is not offered")
        return FileAsset(
            request=ImageRequest(url=_file(slug)),
            mime=MIME_EPUB,
            filename=f"{slug.replace('/', '_')}.epub",
        )
