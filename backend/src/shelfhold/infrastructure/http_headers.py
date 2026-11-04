"""HTTP header values (protocol strings, not user-facing text); single source of truth.

`SecurityHeadersMiddleware` is the only place that applies the discretion headers: route
code adds `Content-Security-Policy`, `Cache-Control` or a sandbox, never the rest.
"""

from typing import Protocol

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
NOSNIFF = "nosniff"
REFERRER_POLICY = "no-referrer"
ROBOTS_TAG = "noindex, nofollow"
FRAME_OPTIONS = "DENY"
FRAME_ANCESTORS = "frame-ancestors 'none'"
CSP_SANDBOX = "sandbox"
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"

_DEFAULTS = {
    "X-Content-Type-Options": NOSNIFF,
    "Referrer-Policy": REFERRER_POLICY,
    "X-Robots-Tag": ROBOTS_TAG,
    "X-Frame-Options": FRAME_OPTIONS,
}


def with_frame_ancestors(csp: str) -> str:
    """A `<meta>` CSP cannot carry `frame-ancestors`; the HTTP header must."""
    if "frame-ancestors" in csp:
        return csp
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
