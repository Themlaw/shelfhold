"""HTTP header values (protocol strings, not user-facing text); single source of truth.

`SecurityHeadersMiddleware` is the only place that applies the discretion headers: route
code adds `Content-Security-Policy`, `Cache-Control` or a sandbox, never the rest.
"""

import base64
import hashlib
import re
from typing import Protocol
from urllib.parse import quote

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
NOSNIFF = "nosniff"
REFERRER_POLICY = "no-referrer"
ROBOTS_TAG = "noindex, nofollow"
FRAME_OPTIONS = "DENY"
FRAME_ANCESTORS = "frame-ancestors 'none'"
CSP_SANDBOX = "sandbox"
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
BEARER_CHALLENGE = 'Bearer realm="api"'
_INLINE = {
    "script-src": re.compile(r"<script>(.*?)</script>", re.DOTALL),
    "style-src": re.compile(r"<style>(.*?)</style>", re.DOTALL),
}

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
    headers["Content-Security-Policy"] = with_frame_ancestors(
        headers.get("Content-Security-Policy", "")
    )


def attachment_disposition(filename: str) -> str:
    """`Content-Disposition: attachment` with an ASCII fallback and an RFC 5987 name."""
    safe = (
        "".join(c for c in filename if c.isascii() and (c.isalnum() or c in "._- ")) or "download"
    )
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _source_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def hash_csp(html: str) -> str:
    """CSP of a served page: nothing by default, its inline scripts and styles by hash only
    (ENF-SEC-004, no `unsafe-inline`); `frame-ancestors` is added by the middleware."""
    directives = ["default-src 'none'"]
    for directive, pattern in _INLINE.items():
        hashes = sorted({_source_hash(body) for body in pattern.findall(html)})
        if hashes:
            directives.append(f"{directive} {' '.join(hashes)}")
    directives += ["connect-src 'self'", "form-action 'self'", "base-uri 'none'"]
    return "; ".join(directives)
