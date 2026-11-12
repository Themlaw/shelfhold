"""HTTP header values (protocol strings, not user-facing text); single source of truth.

`SecurityHeadersMiddleware` is the only place that applies the discretion headers: route
code adds `Content-Security-Policy`, `Cache-Control` or a sandbox, never the rest.
"""

import base64
import hashlib
import re
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import quote

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
NOSNIFF = "nosniff"
REFERRER_POLICY = "no-referrer"
ROBOTS_TAG = "noindex, nofollow"
FRAME_OPTIONS = "DENY"
FRAME_ANCESTORS = "frame-ancestors 'none'"
DATA_CSP = "default-src 'none'"
CSP_SANDBOX = "sandbox"
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
BEARER_CHALLENGE = 'Bearer realm="api"'
_INLINE = {"script-src": "script", "style-src": "style"}

_FRAME_ANCESTORS_DIRECTIVE = re.compile(r"frame-ancestors[^;]*", re.IGNORECASE)

_DEFAULTS = {
    "X-Content-Type-Options": NOSNIFF,
    "Referrer-Policy": REFERRER_POLICY,
    "X-Robots-Tag": ROBOTS_TAG,
    "X-Frame-Options": FRAME_OPTIONS,
}


def with_frame_ancestors(csp: str) -> str:
    """A `<meta>` CSP cannot carry `frame-ancestors`; the HTTP header must."""
    if _FRAME_ANCESTORS_DIRECTIVE.search(csp):
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


def attachment_disposition(filename: str) -> str:
    """`Content-Disposition: attachment` with an ASCII fallback and an RFC 5987 name."""
    safe = (
        "".join(c for c in filename if c.isascii() and (c.isalnum() or c in "._- ")) or "download"
    )
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename, safe='')}"


def source_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def hash_csp(html: str) -> str:
    """CSP of a served page: nothing by default, its inline scripts and styles by hash only
    (ENF-SEC-004, no `unsafe-inline`); `frame-ancestors` is added by the middleware."""
    directives = ["default-src 'none'"]
    page = _Page(html)
    for directive, element in _INLINE.items():
        hashes = sorted({source_hash(body) for body in page.inline[element]})
        if hashes:
            directives.append(f"{directive} {' '.join(hashes)}")
    directives += ["connect-src 'self'", "form-action 'self'", "base-uri 'none'"]
    return "; ".join(directives)


def allow_own_scripts(csp: str) -> str:
    """The same CSP, with `'self'` in `script-src`: a script file of the page's own origin (the
    passkey sign-in). Hashes stay; `unsafe-inline` never appears."""
    return csp.replace("script-src ", "script-src 'self' ", 1)


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
_GUARDED = ("default-src", "script-src", "style-src")


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
    allowed = _directive(csp, "script-src") or _directive(csp, "default-src")
    for body in _inline_scripts(html):
        if source_hash(body) not in allowed:
            raise RuntimeError(
                f"the SPA build has an inline script its CSP does not hash: {body[:40]!r}"
            )
    return csp


_WEB_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)


def require_web_url(url: str) -> str:
    """`source_url` ends in an `href` of every screen: http(s) only, no space, quote or bracket."""
    if _WEB_URL.fullmatch(url) is None:
        raise ValueError("the source address must be an http(s) URL")
    return url
