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


def source_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def hash_csp(html: str) -> str:
    """CSP of a served page: nothing by default, its inline scripts and styles by hash only
    (ENF-SEC-004, no `unsafe-inline`); `frame-ancestors` is added by the middleware."""
    directives = ["default-src 'none'"]
    for directive, pattern in _INLINE.items():
        hashes = sorted({source_hash(body) for body in pattern.findall(html)})
        if hashes:
            directives.append(f"{directive} {' '.join(hashes)}")
    directives += ["connect-src 'self'", "form-action 'self'", "base-uri 'none'"]
    return "; ".join(directives)


def allow_own_scripts(csp: str) -> str:
    """The same CSP, with `'self'` in `script-src`: a script file of the page's own origin (the
    passkey sign-in). Hashes stay; `unsafe-inline` never appears."""
    return csp.replace("script-src ", "script-src 'self' ", 1)


CSP_META = re.compile(
    r'<meta\s+http-equiv="content-security-policy"\s+content="([^"]*)"', re.IGNORECASE
)
INLINE_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.IGNORECASE | re.DOTALL)
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
    return [body for attrs, body in INLINE_SCRIPT.findall(html) if "src=" not in attrs.lower()]


def _fallback_csp(html: str) -> str:
    """A build that carries no CSP of its own is still served under one by hash (ENF-SEC-004)."""
    hashes = " ".join(sorted({source_hash(body) for body in _inline_scripts(html)}))
    scripts = f"script-src 'self' {hashes}".strip()
    directives = ["default-src 'self'", scripts, "style-src 'self'", "object-src 'none'"]
    return "; ".join([*directives, "base-uri 'none'", "form-action 'self'"])


def spa_csp(html: str) -> str:
    """The CSP the SPA is served under: the build's own (its hashes change with each build; the
    middleware adds `frame-ancestors`), checked before a byte is served. A build that loosens a
    script or style source, or whose inline script no hash covers, is refused at start-up: that
    page was not the one audited."""
    match = CSP_META.search(html)
    if match is None:
        return _fallback_csp(html)
    csp = match.group(1)
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
