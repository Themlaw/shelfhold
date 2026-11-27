"""HTTP header values (protocol strings, not user-facing text); single source of truth.

`SecurityHeadersMiddleware` is the only place that applies the discretion headers: route
code adds `Content-Security-Policy`, `Cache-Control` or a sandbox, never the rest.
"""

import base64
import hashlib
import re
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import quote, urlsplit

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
PRIVATE_CACHE = "private, no-cache"  # the bytes of a book: one profile's browser, checked each time
NOSNIFF = "nosniff"
REFERRER_POLICY = "no-referrer"
ROBOTS_TAG = "noindex, nofollow"
FRAME_OPTIONS = "DENY"
FRAME_ANCESTORS = "frame-ancestors 'none'"
# The reader alone: a `blob:` document inherits the policy of its page, `frame-ancestors` included,
# and WebKit checks it for the reading frame (its parent is the page, which `'none'` refuses).
READER_FRAME_ANCESTORS = "frame-ancestors 'self'"
DATA_CSP = "default-src 'none'"
CSP_SANDBOX = "sandbox"
MANIFEST_TYPE = "application/manifest+json"  # the PWA manifest (L16a): not a guess of the platform
# The policy a worker script is served under is the one its global scope runs under: it may read its
# own origin (the shell it keeps) and nothing else, whatever the code of the build says.
WORKER_CSP = "default-src 'none'; connect-src 'self'"
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
BEARER_CHALLENGE = 'Bearer realm="api"'
BASIC_CHALLENGE = 'Basic realm="Shelfhold", charset="UTF-8"'  # the e-reader's OPDS (L46)

_FRAME_ANCESTORS_DIRECTIVE = re.compile(r"frame-ancestors[^;]*", re.IGNORECASE)

_DEFAULTS = {
    "X-Content-Type-Options": NOSNIFF,
    "Referrer-Policy": REFERRER_POLICY,
    "X-Robots-Tag": ROBOTS_TAG,
    "X-Frame-Options": FRAME_OPTIONS,
}


def with_frame_ancestors(csp: str) -> str:
    """A `<meta>` CSP cannot carry `frame-ancestors`; the HTTP header must. Whatever a route wrote
    there becomes `'none'`, except the one exact value of the reader's own policy."""
    found = _FRAME_ANCESTORS_DIRECTIVE.search(csp)
    if found and " ".join(found.group(0).split()).lower() == READER_FRAME_ANCESTORS:
        return csp
    if found:
        return _FRAME_ANCESTORS_DIRECTIVE.sub(FRAME_ANCESTORS, csp)
    return f"{csp}; {FRAME_ANCESTORS}" if csp else FRAME_ANCESTORS


class HeaderBag(Protocol):
    """What Starlette's `MutableHeaders` offers (it is not a `MutableMapping[str, str]`)."""

    def get(self, key: str, default: str, /) -> str: ...

    def __setitem__(self, key: str, value: str) -> None: ...


def apply_security_headers(headers: HeaderBag) -> None:
    """Fix the discretion headers; extend (never replace) a CSP set by the route."""
    for name, value in _DEFAULTS.items():
        headers[name] = value
    csp = headers.get("Content-Security-Policy", "")
    media_type = headers.get("Content-Type", "").split(";")[0].strip().lower()
    if not csp and (media_type == "application/json" or media_type.endswith("+json")):
        csp = DATA_CSP  # a JSON or problem+json answer loads nothing, whatever it is made to hold
    headers["Content-Security-Policy"] = with_frame_ancestors(csp)


NO_STORE = "no-store"
API_PREFIX = "/api/v1/"


def apply_api_cache_default(headers: HeaderBag, path: str) -> None:
    """A JSON answer of the API is kept in no cache (a shared one or the browser's) unless its
    route set its own policy: a `Cache-Control`, or an `ETag` its client revalidates with."""
    if not path.startswith(API_PREFIX):
        return
    media_type = headers.get("Content-Type", "").split(";")[0].strip().lower()
    if not (media_type == "application/json" or media_type.endswith("+json")):
        return
    if headers.get("Cache-Control", "") or headers.get("ETag", ""):
        return
    headers["Cache-Control"] = NO_STORE


def attachment_disposition(filename: str) -> str:
    """`Content-Disposition: attachment` with an ASCII fallback and an RFC 5987 name."""
    safe = (
        "".join(c for c in filename if c.isascii() and (c.isalnum() or c in "._- ")) or "download"
    )
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename, safe='')}"


def source_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def page_csp(*, scripts: bool = False, images: bool = False) -> str:
    """The policy of a page the server writes itself, which links its own files (style sheet,
    fonts, scripts): styles and fonts from this origin, scripts only when the page has some (still
    this origin), covers only when asked, no inline code and no other host. `frame-ancestors` is
    added by the middleware."""
    directives = ["default-src 'none'", "style-src 'self'", "font-src 'self'"]
    if scripts:
        directives += ["script-src 'self'", "connect-src 'self'"]
    if images:
        directives.append("img-src 'self'")
    directives += ["form-action 'self'", "base-uri 'none'"]
    return "; ".join(directives)


class _Page(HTMLParser):
    """What a page carries itself, read by a real HTML parser (a regex on tags is bypassed by
    `</script >`, case, attributes or a comment): its inline script and style bodies, and the CSP
    of its `<meta http-equiv>`. Comments and CDATA are the parser's, never taken for elements."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=False)
        self.inline: dict[str, list[str]] = {"script": [], "style": []}
        self.meta_csp: str | None = None
        self._open: str | None = None
        self._text: list[str] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "meta" and values.get("http-equiv", "").lower() == "content-security-policy":
            if self.meta_csp is None:
                self.meta_csp = values.get("content", "")
        elif tag in self.inline and "src" not in values:
            self._open, self._text = tag, []

    def handle_data(self, data: str) -> None:
        if self._open:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._open:
            self.inline[tag].append("".join(self._text))
            self._open = None


def meta_csp(html: str) -> str | None:
    """The CSP the build carries in its `<meta http-equiv>`, if any."""
    return _Page(html).meta_csp


_LOOSE_SOURCES = ("'unsafe-inline'", "'unsafe-eval'", "*", "https:", "http:", "data:", "blob:")
_GUARDED = ("default-src", "script-src", "style-src", "worker-src", "manifest-src")  # L16a: the PWA
_IMAGE_SOURCES = (
    "'self'",
)  # the reader (own entry, own policy: `reader_csp`) draws the `blob:` ones


def _directive(csp: str, name: str) -> list[str]:
    for part in csp.split(";"):
        words = part.split()
        if words and words[0].lower() == name:
            return words[1:]
    return []


def _inline_scripts(html: str) -> list[str]:
    """The body of every script the page carries itself, which a CSP by hash must name."""
    return _Page(html).inline["script"]


def spa_csp(html: str) -> str:
    """The CSP the SPA is served under: the build's own (its hashes change with each build; the
    middleware adds `frame-ancestors`), checked before a byte is served. A build that loosens a
    script or style source, or whose inline script no hash covers, is refused at start-up: that
    page was not the one audited."""
    csp = meta_csp(html)
    if csp is None:
        raise RuntimeError("the SPA build carries no CSP meta: refusing to serve it")
    for name in _GUARDED:
        loose = [v for v in _directive(csp, name) if v.lower() in _LOOSE_SOURCES]
        if loose:
            raise RuntimeError(f"the CSP of the SPA build is loose: {name} allows {loose}")
    wider = [v for v in _directive(csp, "img-src") if v.lower() not in _IMAGE_SOURCES]
    if wider:
        raise RuntimeError(f"the CSP of the SPA build is loose: img-src allows {wider}")
    allowed = _directive(csp, "script-src") or _directive(csp, "default-src")
    for body in _inline_scripts(html):
        if source_hash(body) not in allowed:
            raise RuntimeError(
                f"the SPA build has an inline script its CSP does not hash: {body[:40]!r}"
            )
    return csp


_PRELOADS = frozenset(
    {"modulepreload", "preload", "prefetch", "preconnect", "dns-prefetch", "prerender"}
)
_REMOTE = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)
_READER_ASSETS = "/reader-entry/"
_READER_TABLE = (
    ("default-src", ("'none'",)),
    (
        "script-src",
        (),
    ),  # the hash of the boot script, then `'strict-dynamic'`: filled from the page
    ("style-src", ("'self'", "blob:", "'unsafe-inline'")),
    ("style-src-elem", ("'self'", "blob:", "'unsafe-inline'")),
    ("style-src-attr", ("'unsafe-inline'",)),
    ("img-src", ("'self'", "blob:")),
    ("font-src", ("'self'", "blob:")),
    ("media-src", ("'none'",)),
    ("frame-src", ("blob:",)),
    ("worker-src", ("'none'",)),
    ("manifest-src", ("'none'",)),
    ("connect-src", ("'self'",)),
    ("object-src", ("'none'",)),
    ("base-uri", ("'none'",)),
    ("form-action", ("'none'",)),
    ("frame-ancestors", ("'self'",)),
)


class _ReaderAudit(HTMLParser):
    """What the page of the reader must not hold, read by a real HTML parser: a `<meta http-equiv>`
    (its policy would stack on the header), a script file, a preload (refused under
    `'strict-dynamic'`), a link that is not its own style sheet, a handler, a remote address."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=False)
        self.problems: list[str] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        if tag == "meta" and "http-equiv" in values:
            self.problems.append("it carries a <meta http-equiv>")
        if tag == "script" and "src" in values:
            self.problems.append("it loads a script file")
        if tag == "link":
            rel = values.get("rel", "").lower().split()
            if any(r in _PRELOADS for r in rel):
                self.problems.append("it holds a preload")
            elif "stylesheet" not in rel or not values.get("href", "").startswith(_READER_ASSETS):
                self.problems.append(
                    f"a link that is not its own style sheet: {values.get('href')}"
                )
        for name, value in values.items():
            if name.startswith("on"):
                self.problems.append(f"a handler: {name}")
            if name in {"src", "href", "action", "data", "poster"} and _REMOTE.match(value):
                self.problems.append(f"a remote address: {name}")


def reader_csp(html: str) -> str:
    """The CSP of `/lire` (ADR-0055, "CSP du lecteur, directive par directive"), written from the
    table of the ADR and from the very page the build produced: its one inline script is hashed,
    `'strict-dynamic'` trusts what it imports and nothing a book holds. The page is the reader's
    own entry, outside the router, with no `<meta>` policy; one that is not the audited page is
    refused at start-up (as `spa_csp` does for the SPA). `frame-ancestors 'self'` is the one the
    middleware lets through (see `READER_FRAME_ANCESTORS`)."""
    audit = _ReaderAudit(html)
    if audit.problems:
        raise RuntimeError(f"the reader page is not the audited one: {audit.problems[0]}")
    scripts = _inline_scripts(html)
    if len(scripts) != 1:
        raise RuntimeError(
            f"the reader page is not the audited one: it has {len(scripts)} inline scripts, not one"
        )
    directives = []
    for name, sources in _READER_TABLE:
        values = (source_hash(scripts[0]), "'strict-dynamic'") if name == "script-src" else sources
        directives.append(f"{name} {' '.join(values)}")
    return "; ".join(directives)


_WEB_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)


def require_web_url(url: str) -> str:
    """`source_url` ends in an `href` of every screen: http(s) only, no space, quote or bracket."""
    if _WEB_URL.fullmatch(url) is None:
        raise ValueError("the source address must be an http(s) URL")
    return url


def require_plain_web_url(url: str) -> str:
    """An address the instance will call (update check, plugin index): http(s), and nothing in it
    that is a credential: no `user:password@`, no query or fragment (a token travels there)."""
    require_web_url(url)
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("the address must not carry credentials")
    if parts.query or parts.fragment or not parts.hostname:
        raise ValueError("the address must be a plain http(s) URL: no query, no fragment")
    return url
