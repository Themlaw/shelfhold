"""HTTP header values for the static SPA (protocol strings, not user-facing text)."""

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
NOSNIFF = "nosniff"


def with_frame_ancestors(csp: str) -> str:
    """A `<meta>` CSP cannot carry `frame-ancestors`; the HTTP header must."""
    return f"{csp}; frame-ancestors 'none'"
