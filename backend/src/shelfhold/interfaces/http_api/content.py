"""Responses for untrusted content (covers, upstream files, the bytes of a book): sandboxed, never
rendered inline unless a raster image (ENF-SEC-004)."""

from fastapi.responses import Response

from shelfhold.infrastructure.http_headers import (
    CSP_SANDBOX,
    PRIVATE_CACHE,
    attachment_disposition,
)

_INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"})


def content_response(
    body: bytes, *, media_type: str, filename: str, etag: str | None = None
) -> Response:
    """`CSP: sandbox` always; `attachment` unless a raster image (SVG and HTML are an attachment
    without exception); `Cache-Control: private`. The discretion headers (`nosniff`,
    `Referrer-Policy`, `frame-ancestors`) are the middleware's."""
    response = Response(body, media_type=media_type)
    response.headers["Content-Security-Policy"] = CSP_SANDBOX
    response.headers["Cache-Control"] = PRIVATE_CACHE
    if etag is not None:
        response.headers["ETag"] = etag
    if media_type.split(";", 1)[0].strip().lower() not in _INLINE_TYPES:
        response.headers["Content-Disposition"] = attachment_disposition(filename)
    return response
