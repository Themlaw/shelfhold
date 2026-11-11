"""The Wikisource source (EF-SRC-011, EF-BIB-033, ENF-LEG-003).

A book is a page of the main namespace of one language's Wikisource, taken as the EPUB the wiki's
own export tool makes of it: one Series, one Unit, one file (D13: a one-shot acquisition). The
source asks two places and no more: the MediaWiki API (`/w/` of a `*.wikisource.org` host) to
search and to read who contributed to a page, and the export tool for the EPUB. It never reads a
page of the wiki, and the DomainRules of the manifest refuse any other host and any other path.

The text of Wikisource is shared under CC BY-SA: the licence asks that its contributors, the
licence and a link to the origin travel with it. The source builds that attribution as plain text
(`SeriesInfo.rights`); the host stores it on the Edition and copies it into the exported EPUB
(`dc:rights`, `dc:source`) even when the file carries a `dc:rights` of its own that omits it.

Contributor names are written by anyone: a name that holds markup, a link or a line break is left
out and counted among "others", a long name is cut, and the list is bounded. The EPUB is always
`untrusted`: a page can hold anything, and the host neutralizes it before any export.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final
from urllib.parse import quote, unquote, urlencode, urlsplit

from shelfhold.sdk.fetcher import Fetcher, Response
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

__all__ = ["KEY", "WikisourceSource", "build_manifest", "page_url", "parse_remote_id"]

KEY: Final = "wikisource"
PUBLISHER: Final = "Wikisource"
PAGE_SIZE: Final = 25
MAX_CONTRIBUTORS: Final = 25  # names written in the attribution; the others are counted
MAX_NAME: Final = 80
MAX_TITLE: Final = 255
DEFAULT_LANGUAGE: Final = "fr"
MIME_EPUB: Final = "application/epub+zip"
LICENCE: Final = "CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/)"
API_HOST: Final = "{lang}.wikisource.org"
EXPORT_URL: Final = "https://ws-export.wmcloud.org/"

# A language sub-domain: letters, with dashes between groups (`fr`, `be-tarask`). Nothing that
# could name another host, a port, a path or a query.
_LANG = re.compile(r"[a-z]{2,3}(?:-[a-z]{2,8}){0,2}")
_BAD_TITLE = re.compile(r"[\x00-\x1f\x7f#|<>\[\]{}\\]")
_BAD_NAME = re.compile(r"[\x00-\x1f\x7f<>]|://|www\.", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_WIKI = re.compile(r"https?://(?P<lang>[^/?#]+)\.wikisource\.org/wiki/(?P<title>[^?#]*)", re.I)


def build_manifest() -> SourceManifest:
    """Same content as `sources/wikisource/manifest.json` (a test keeps the two equal)."""
    return SourceManifest(
        key=KEY,
        kind=PluginKind.SOURCE,
        version="0.1.0",
        api=1,
        name="Wikisource (one EPUB per book, with its attribution)",
        permissions=Permissions(
            domains=[
                DomainRule(pattern="*.wikisource.org", purpose="api", path_prefix="/w/"),
                DomainRule(pattern="ws-export.wmcloud.org", purpose="other"),
            ]
        ),
        settings_schema={
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "title": "Language of the Wikisource searched by default (code, e.g. fr)",
                    "default": DEFAULT_LANGUAGE,
                },
            },
        },
        content_warning="mixed",
        terms_url="https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
        respects_robots=True,
        attribution=(
            "Texts from Wikisource, shared under CC BY-SA 4.0: the contributors of the "
            "Wikisource page, the licence and a link to the page go with the file."
        ),
        langs=[],
        content_kinds={"file"},
        url_patterns=["https://*.wikisource.org/wiki/**"],
        capabilities=Capability.SEARCH | Capability.URL_RESOLVE,
        rate_limit=RateLimit(requests_per_second=1.0, concurrency=1),
        rights_basis="licence",
        translation_nature="unknown",
        content_trust="untrusted",
    )


def _valid_language(value: object) -> str:
    text = value if isinstance(value, str) else ""
    if _LANG.fullmatch(text) is None or len(text) > 20:
        raise ContentRemoved("not the code of a Wikisource language")
    return text


def _valid_title(raw: str) -> str:
    title = raw.replace(" ", "_").strip("_")
    namespaced = ":" in title.split("/", 1)[0]
    if not title or len(title) > MAX_TITLE or _BAD_TITLE.search(title) or namespaced:
        raise ContentRemoved("not the title of a book page")
    if title.startswith("/") or "//" in title:
        raise ContentRemoved("not the title of a book page")
    return title


def parse_remote_id(remote_id: str) -> tuple[str, str]:
    """`(language, title)` of a reference `fr:Titre_du_livre`, or a refusal."""
    language, sep, title = remote_id.partition(":")
    if not sep:
        raise ContentRemoved("not a reference of this source")
    return _valid_language(language), _valid_title(title)


def page_url(language: str, title: str) -> str:
    return f"https://{API_HOST.format(lang=language)}/wiki/{quote(title, safe='/:()!,;@$*~')}"


def _name(raw: object) -> str | None:
    if not isinstance(raw, str) or _BAD_NAME.search(raw):
        return None
    text = _SPACES.sub(" ", raw).strip()
    return text[:MAX_NAME] if text else None


def attribution(
    language: str, title: str, names: list[str], *, anonymous: int, truncated: bool
) -> str:
    """The attribution the licence asks for, as plain text: the origin link first (the host takes
    the first link as `dc:source`), then the licence, then who wrote the page."""
    shown = names[:MAX_CONTRIBUTORS]
    hidden = len(names) - len(shown) + anonymous
    who = ", ".join(shown) if shown else "anonymous contributors"
    if hidden > 0:
        who += f" and {hidden} other contributors"
    elif truncated:
        who += " and other contributors"
    return (
        f"Source: {page_url(language, title)}. Licence: {LICENCE}. "
        f"Contributors of Wikisource (a book assembled from other pages may have more: see the "
        f"history of the page): {who}."
    )


class WikisourceSource:
    def __init__(self) -> None:
        self.manifest = build_manifest()

    def filters(self) -> list[FilterSpec]:
        return [FilterSpec(key="language", kind="text", label="Language (code)")]

    # -- the API ----------------------------------------------------------------------------------

    async def _api(self, f: Fetcher, language: str, params: dict[str, Any]) -> dict[str, Any]:
        host = API_HOST.format(lang=_valid_language(language))
        answer = await f.get(
            f"https://{host}/w/api.php", params={**params, "format": "json", "formatversion": "2"}
        )
        _check(answer)
        try:
            body = json.loads(answer.body)
        except ValueError as error:
            raise SourceChanged("the API did not answer with JSON") from error
        if not isinstance(body, dict) or "error" in body:
            raise SourceChanged("the API refused the request")
        return body

    # -- the contract -----------------------------------------------------------------------------

    async def search(
        self,
        f: Fetcher,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        language = _valid_language(
            filters.get("language") or f.settings().get("language") or DEFAULT_LANGUAGE
        )
        offset = int(cursor.token) if cursor is not None and cursor.token.isdigit() else 0
        body = await self._api(
            f,
            language,
            {
                "action": "query", "list": "search", "srsearch": q[:300], "srnamespace": "0",
                "srlimit": str(PAGE_SIZE), "sroffset": str(offset),
            },
        )  # fmt: skip
        found = body.get("query", {}).get("search", [])
        refs: list[SeriesRef] = []
        for item in found if isinstance(found, list) else []:
            title = item.get("title") if isinstance(item, dict) else None
            try:
                clean = _valid_title(title) if isinstance(title, str) else None
            except ContentRemoved:
                continue  # a title this source would not read is not offered
            if clean is not None and item.get("ns", 0) == 0:
                refs.append(
                    SeriesRef(source_key=KEY, remote_id=f"{language}:{clean}", title=title or "")
                )
        following = body.get("continue", {}).get("sroffset")
        more = isinstance(following, int) and following > offset
        return Page[SeriesRef](items=refs, next=Cursor(token=str(following)) if more else None)

    async def resolve_url(self, f: Fetcher, url: str) -> SeriesRef | UnitInfo | None:
        """The book a pasted address names, without asking anything: a page of a Wikisource."""
        try:
            parts = urlsplit(url)
            if parts.scheme.lower() not in ("http", "https") or parts.port not in (None, 80, 443):
                return None
            found = _WIKI.fullmatch(f"{parts.scheme}://{parts.netloc}{parts.path}")
            if found is None or "@" in parts.netloc:
                return None
            language = _valid_language(found["lang"].lower())
            title = _valid_title(unquote(found["title"]))
        except (ContentRemoved, ValueError):
            return None
        return SeriesRef(source_key=KEY, remote_id=f"{language}:{title}", title=title)

    async def get_series(self, f: Fetcher, ref: SeriesRef) -> SeriesInfo:
        language, title = parse_remote_id(ref.remote_id)
        body = await self._api(
            f,
            language,
            {
                "action": "query", "prop": "contributors", "titles": title.replace("_", " "),
                "pclimit": str(MAX_CONTRIBUTORS * 2), "redirects": "1",
            },
        )  # fmt: skip
        pages = body.get("query", {}).get("pages", [])
        page = pages[0] if isinstance(pages, list) and pages and isinstance(pages[0], dict) else {}
        if page.get("missing") or page.get("invalid") or page.get("ns", 0) != 0:
            raise ContentRemoved("no such book page in the main namespace")
        names = [n for c in page.get("contributors") or [] if (n := _name(c.get("name")))]
        dropped = len(page.get("contributors") or []) - len(names)
        anonymous = page.get("anoncontributors")
        count = (anonymous if isinstance(anonymous, int) and anonymous > 0 else 0) + dropped
        shown = title.replace("_", " ")
        return SeriesInfo(
            ref=ref,
            title=shown,
            publisher=PUBLISHER,
            external_ids={"wikisource": f"{language}:{title}"},
            rights=attribution(
                language, title, names, anonymous=count, truncated="continue" in body
            ),
        )

    async def list_units(self, f: Fetcher, ref: SeriesRef) -> list[UnitInfo]:
        language, title = parse_remote_id(ref.remote_id)
        return [
            UnitInfo(
                remote_unit_id=f"{KEY}:{language}:{title}:epub",
                source_order=1,
                kind="book",
                title=title.replace("_", " "),
                lang=language,  # type: ignore[arg-type]
                lang_raw=language,
            )
        ]

    async def fetch_unit(self, f: Fetcher, ref: SeriesRef, u: UnitInfo) -> FileAsset:
        language, title = parse_remote_id(ref.remote_id)
        if u.remote_unit_id != f"{KEY}:{language}:{title}:epub":
            raise ContentRemoved("that Unit is not offered")
        query = urlencode({"format": "epub-3", "lang": language, "page": title})
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title)[:100].strip("._") or "wikisource"
        return FileAsset(
            request=ImageRequest(url=f"{EXPORT_URL}?{query}"),
            mime=MIME_EPUB,
            filename=f"{stem}.epub",
        )


def _check(answer: Response) -> None:
    if answer.status == 429:
        raise RateLimited("the wiki asked to slow down")
    if answer.status in (404, 410):
        raise ContentRemoved("the wiki has no such document")
    if answer.status >= 500:
        raise TemporarilyUnavailable(f"the wiki answered {answer.status}")
    if answer.status != 200:
        raise SourceChanged(f"the wiki answered {answer.status}")
