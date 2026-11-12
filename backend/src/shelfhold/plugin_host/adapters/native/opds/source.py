"""The generic OPDS source (EF-SRC-006): any catalogue that speaks OPDS 1.2 (Atom) or 2.0 (JSON)
with OpenSearch: Kavita, Komga, Calibre-web, a library's own.

What this adapter does, and what it leaves to the host:

- it reads the catalogue the admin configured (`base_url`) and nothing else: every address it is
  about to fetch (a link, a cursor, an OpenSearch template, an acquisition link) is checked to be
  of that origin first, so a feed can neither lead it, nor the credential, anywhere else. The
  Fetcher checks the DomainRules and `robots.txt` again at every hop;
- it never sees a credential: Basic and ApiKey are injected by the host into the Fetcher it is
  handed. A key that a catalogue writes into the links it serves is rewritten to `{secret}` by
  the host before any reference is kept;
- it never walks a catalogue on its own: a search reads one page and hands back the cursor; the
  pages of a series are read up to `MAX_PAGES`, and a loop or a longer chain is a refusal;
- the file it hands over is `untrusted` (the manifest says so, and nothing a feed says changes it):
  an EPUB takes the neutralizer, and the anti-echo rule (I50) turns the host's own exports, when a
  catalogue serves them back, into replicas.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import unquote, urlsplit

from shelfhold.plugin_host.adapters.native.opds import json as opds_json
from shelfhold.plugin_host.adapters.native.opds import opensearch
from shelfhold.plugin_host.adapters.native.opds.atom import ParseXml, parse_atom
from shelfhold.plugin_host.adapters.native.opds.model import (
    Entry,
    Feed,
    Link,
    absolute,
    media_type,
    origin_of,
)
from shelfhold.sdk.fetcher import Fetcher, Response
from shelfhold.sdk.models import API_VERSION as API
from shelfhold.sdk.models import (
    AuthApiKey,
    AuthBasic,
    Cursor,
    FilterSpec,
    Page,
    PluginKind,
    RateLimit,
)
from shelfhold.sdk.refs import FileAsset, ImageRequest, SeriesInfo, SeriesRef, UnitInfo
from shelfhold.sdk.source import (
    AuthRequired,
    Capability,
    ContentRemoved,
    NotConfigured,
    RateLimited,
    SourceChanged,
    SourceManifest,
    TemporarilyUnavailable,
)

__all__ = ["KEY", "MAX_PAGES", "NotConfigured", "OpdsSource", "build_manifest"]

KEY: Final = "opds"
MAX_PAGES: Final = 20  # pages of one feed read in a row: a series longer than that is refused
SEARCH_REL: Final = "search"
OPDS_JSON: Final = ("application/opds+json", "application/json")
_KEY_IN_PATH = re.compile(
    r"/api/opds/(?!\{)(?:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}|[0-9A-Za-z_-]{16,})(?:/|$)"
)
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_NAME: Final = 120
SETTINGS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["base_url"],
    "properties": {
        "base_url": {"type": "string", "title": "Catalogue address (the OPDS root)"},
        "auth": {
            "type": "string",
            "title": "Authentication",
            "enum": ["none", "basic", "api_key_header", "api_key_query", "api_key_path"],
            "default": "none",
        },
        "username": {"type": "string", "title": "User name (basic)"},
        "password": {"type": "string", "format": "password", "title": "Password (basic)"},
        "api_key": {"type": "string", "format": "password", "title": "API key"},
    },
}


def build_manifest() -> SourceManifest:
    """Same content as `sources/opds/manifest.json` (a test keeps the two equal)."""
    from shelfhold.sdk.models import DomainRule, Permissions

    return SourceManifest(
        key=KEY,
        kind=PluginKind.SOURCE,
        version="0.1.0",
        api=API,
        name="OPDS catalogue (Kavita, Komga, Calibre-web, ...)",
        terms_url="https://github.com/Themlaw/shelfhold/blob/main/docs/admin/sources.md",
        permissions=Permissions(
            # the host is the one the admin configured: `opds.invalid` reaches nothing alone
            domains=[DomainRule(pattern="opds.invalid", purpose="api", base_url_setting="base_url")]
        ),
        settings_schema=SETTINGS_SCHEMA,
        content_warning="mixed",
        langs=[],
        content_kinds={"file"},
        url_patterns=[],
        capabilities=Capability.SEARCH,
        rate_limit=RateLimit(requests_per_second=2.0, concurrency=1),
        auth=[
            AuthBasic(),
            AuthApiKey(location="header", name="X-API-Key"),
            AuthApiKey(location="query", name="apikey"),
            AuthApiKey(location="path", name="apiKey", ref_template="{secret}"),
        ],
        rights_basis="user_supplied",
        translation_nature="unknown",
        content_trust="untrusted",  # a third party's catalogue: the neutralizer runs on its EPUBs
    )


def _memo_of(entry: Entry) -> dict[str, Any]:
    """What a result keeps of its entry: which one, and the subjects the feed gives it (the key
    `"subjects"` the family filter reads)."""
    memo: dict[str, Any] = {"entry": entry.id}
    if entry.categories:
        memo["subjects"] = list(entry.categories)
    return memo


class OpdsSource:
    def __init__(self, parse_xml: ParseXml, *, base_url: str | None = None) -> None:
        self.manifest = build_manifest()
        self._parse_xml = parse_xml
        self._base = base_url
        self._seen: dict[str, Feed] = {}  # what this handle already read: one operation's memo

    # -- configuration -------------------------------------------------------------------------

    def configured(self, settings: Mapping[str, Any]) -> OpdsSource:
        """A copy bound to the admin's address (the plain settings; a secret is never given)."""
        raw = settings.get("base_url")
        if not isinstance(raw, str) or not raw.strip():
            raise NotConfigured("the catalogue address is not set")
        text = raw.strip()
        try:
            parts = urlsplit(text)
            _ = parts.port
        except ValueError:
            raise NotConfigured("the catalogue address is not a URL") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise NotConfigured("the catalogue address must be http(s)://host")
        if "@" in parts.netloc or parts.fragment:
            # a credential belongs in the secret settings, never in an address kept in clear
            raise NotConfigured("the catalogue address holds credentials or a fragment")
        if settings.get("auth") != "api_key_path" and _KEY_IN_PATH.search(parts.path):
            # a key written in the address would sit in clear in the settings, references, logs
            raise NotConfigured("the catalogue address holds a key: use the api_key_path flow")
        return OpdsSource(self._parse_xml, base_url=text.rstrip("/"))

    def filters(self) -> list[FilterSpec]:
        return []

    # -- reading -------------------------------------------------------------------------------

    @property
    def _root(self) -> str:
        if self._base is None:
            raise NotConfigured("the catalogue address is not set")
        return self._base

    def _inside(self, url: str | None) -> str:
        """`url` when it is of the catalogue's origin, else a refusal: nothing is fetched at an
        address a feed, a cursor or a reference chose outside it."""
        resolved = absolute(self._root + "/", url or "")
        if resolved is None or origin_of(resolved) != origin_of(self._root):
            raise SourceChanged("an address leaves the catalogue")
        return resolved

    def _relative(self, url: str) -> str:
        parts = urlsplit(self._inside(url))
        return parts.path + (f"?{parts.query}" if parts.query else "")

    async def _get(self, f: Fetcher, url: str) -> Response:
        response = await f.get(self._inside(url))
        status = response.status
        if status in (401, 403):
            raise AuthRequired("the catalogue refused the credentials")
        if status == 404 or status == 410:
            raise ContentRemoved("the catalogue has no such document")
        if status == 429:
            raise RateLimited("the catalogue asks to slow down", _retry_after(response))
        if status >= 500:
            raise TemporarilyUnavailable("the catalogue is not answering")
        if not 200 <= status < 300:
            raise SourceChanged("the catalogue answered an unexpected status")
        return response

    async def _read(self, f: Fetcher, url: str) -> Feed:
        url = self._inside(url)
        if url in self._seen:
            return self._seen[url]
        response = await self._get(f, url)
        mime = media_type(
            next((v for k, v in response.headers.items() if k.lower() == "content-type"), "")
        )
        body = response.body
        if mime in OPDS_JSON or body.lstrip()[:1] == b"{":
            feed = opds_json.parse_opds2(body, url)
        else:
            feed = parse_atom(body, url, self._parse_xml)
        self._seen[url] = feed
        return feed

    async def _pages(self, f: Fetcher, url: str) -> list[Feed]:
        """The pages of a feed, `next` after `next`, up to `MAX_PAGES`; a page met twice or a
        chain that does not end is a refusal."""
        pages: list[Feed] = []
        visited: set[str] = set()
        current: str | None = self._inside(url)
        while current is not None:
            if current in visited or len(pages) >= MAX_PAGES:
                raise SourceChanged("the feed pages do not end")
            visited.add(current)
            feed = await self._read(f, current)
            pages.append(feed)
            current = self._inside(feed.next) if feed.next else None
        return pages

    # -- search --------------------------------------------------------------------------------

    async def search(
        self,
        f: Fetcher,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        if cursor is not None:
            url = self._inside(cursor.token)
        else:
            url = await self._search_url(f, q)
        feed = await self._read(f, url)
        items = [ref for e in feed.entries if (ref := self._ref(e, url)) is not None]
        following = self._inside(feed.next) if feed.next and feed.next != url else None
        return Page[SeriesRef](
            items=items, next=Cursor(token=following) if following is not None else None
        )

    async def _search_url(self, f: Fetcher, q: str) -> str:
        root = await self._read(f, self._root)
        link = root.search
        if link is None:
            raise SourceChanged("the catalogue offers no search")
        target = self._inside(link.href)
        if link.templated or "{" in link.href:
            return self._inside(opensearch.expand(link.href, q))
        if media_type(link.type) == "application/opensearchdescription+xml" or not link.type:
            response = await self._get(f, target)
            chosen = opensearch.pick_atom(
                opensearch.parse_description(response.body, target, self._parse_xml)
            )
            if chosen is None:
                raise SourceChanged("the description holds no OPDS search")
            return self._inside(opensearch.expand(chosen.template, q))
        return self._inside(opensearch.expand(target + "{?query}", q))

    def _ref(self, entry: Entry, feed_url: str) -> SeriesRef | None:
        title = entry.title or entry.id
        if entry.subsection is not None:
            return SeriesRef(
                source_key=KEY, remote_id=self._relative(entry.subsection), title=title
            )
        if entry.acquisitions or entry.locked:
            return SeriesRef(
                source_key=KEY, remote_id=self._relative(feed_url), title=title,
                memo=_memo_of(entry),
            )  # fmt: skip
        return None

    # -- one series and its books -----------------------------------------------------------------

    async def get_series(self, f: Fetcher, ref: SeriesRef) -> SeriesInfo:
        pages = await self._pages(f, self._inside(ref.remote_id))
        wanted = ref.memo.get("entry")
        entries = [e for p in pages for e in p.entries if wanted is None or e.id == wanted]
        first = entries[0] if entries else None
        title = ref.title or (first.title if first else "") or pages[0].title
        return SeriesInfo(
            ref=ref, title=title, description=(first.summary or None) if first else None,
            authors=list(first.authors) if first else [],
        )  # fmt: skip

    async def list_units(self, f: Fetcher, ref: SeriesRef) -> list[UnitInfo]:
        wanted = ref.memo.get("entry")
        units: list[UnitInfo] = []
        known: set[str] = set()
        for page in await self._pages(f, self._inside(ref.remote_id)):
            for entry in page.entries:
                if wanted is not None and entry.id != wanted:
                    continue
                unit = self._unit(entry, len(units))
                if unit is not None and unit.remote_unit_id not in known:
                    known.add(unit.remote_unit_id)
                    units.append(unit)
        return units

    def _unit(self, entry: Entry, order: int) -> UnitInfo | None:
        if not entry.acquisitions and not entry.locked:
            return None
        offered = [k for k in entry.acquisitions if self._of_catalogue(k.href)]
        if entry.acquisitions and not offered:
            return None  # every link leaves the catalogue: not offered, never followed
        return UnitInfo(
            remote_unit_id=entry.id or entry.digest, source_order=order, kind="book",
            title=entry.title or None, lang=entry.language or None,  # type: ignore[arg-type]
            lang_raw=entry.language or None,
            availability="free" if offered else "locked",
            revision_hint=entry.digest,  # the entry as received: a change shows as a new revision
        )  # fmt: skip

    def _of_catalogue(self, url: str) -> bool:
        return origin_of(url) == origin_of(self._root)

    async def fetch_unit(self, f: Fetcher, ref: SeriesRef, u: UnitInfo) -> FileAsset:
        wanted = ref.memo.get("entry")
        for page in await self._pages(f, self._inside(ref.remote_id)):
            for entry in page.entries:
                if (wanted is not None and entry.id != wanted) or (
                    entry.id or entry.digest
                ) != u.remote_unit_id:
                    continue
                for link in entry.acquisitions:
                    if self._of_catalogue(link.href):
                        return self._asset(entry, link)
        raise ContentRemoved("the book is no longer offered")

    def _asset(self, entry: Entry, link: Link) -> FileAsset:
        return FileAsset(
            request=ImageRequest(url=self._inside(link.href)),
            mime=media_type(link.type), filename=_filename(entry, link),
        )  # fmt: skip


def _retry_after(response: Response) -> float | None:
    raw = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), "")
    try:
        return max(0.0, min(float(raw), 3600.0))
    except ValueError:
        return None


def _filename(entry: Entry, link: Link) -> str:
    leaf = unquote(urlsplit(link.href).path.rsplit("/", 1)[-1])
    extension = ".epub" if media_type(link.type) == "application/epub+zip" else ".cbz"
    stem = leaf.rsplit(".", 1)[0] if "." in leaf else leaf
    name = _UNSAFE_NAME.sub("_", stem or entry.title or "livre").strip(" .") or "livre"
    return name[: MAX_NAME - len(extension)] + extension
