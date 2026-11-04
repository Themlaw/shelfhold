"""Responses for untrusted content (covers, upstream files): sandboxed, never rendered inline."""

from fastapi.responses import Response

from shelfhold.infrastructure.http_headers import CSP_SANDBOX, attachment_disposition

_INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"})


def content_response(body: bytes, *, media_type: str, filename: str) -> Response:
    """`CSP: sandbox` always; `attachment` unless a raster image (ENF-SEC-004)."""
    response = Response(body, media_type=media_type)
    response.headers["Content-Security-Policy"] = CSP_SANDBOX
    if media_type.split(";", 1)[0].strip().lower() not in _INLINE_TYPES:
        response.headers["Content-Disposition"] = attachment_disposition(filename)
    return response
