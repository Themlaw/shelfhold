"""Responses for untrusted content (covers, upstream files): sandboxed, never rendered inline."""

from fastapi.responses import Response

from shelfhold.infrastructure.http_headers import CSP_SANDBOX, attachment_disposition

_ATTACHMENT_TYPES = frozenset({"image/svg+xml", "text/html", "application/xhtml+xml"})


def content_response(body: bytes, *, media_type: str, filename: str) -> Response:
    """`CSP: sandbox` always; `Content-Disposition: attachment` for SVG and HTML (ENF-SEC-004)."""
    response = Response(body, media_type=media_type)
    response.headers["Content-Security-Policy"] = CSP_SANDBOX
    if media_type.split(";", 1)[0].strip().lower() in _ATTACHMENT_TYPES:
        response.headers["Content-Disposition"] = attachment_disposition(filename)
    return response
