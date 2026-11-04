"""Serves the statically built SPA (SvelteKit adapter-static, `fallback: index.html`)."""

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response

from shelfhold.infrastructure.http_headers import (
    IMMUTABLE_CACHE,
    NO_CACHE,
    NOSNIFF,
    with_frame_ancestors,
)

API_SEGMENTS = frozenset({"api", "healthz", "readyz", "openapi.json"})
IMMUTABLE_PREFIX = "_app/immutable/"
CSP_META = re.compile(
    r'<meta\s+http-equiv="content-security-policy"\s+content="([^"]*)"', re.IGNORECASE
)


def _header_csp(index: Path) -> str | None:
    """The build's CSP (hashes change with each build), plus what a meta tag cannot carry."""
    match = CSP_META.search(index.read_text(encoding="utf-8"))
    return with_frame_ancestors(match.group(1)) if match else None


def mount_spa(app: FastAPI, build_dir: Path) -> bool:
    """Register the SPA catch-all after the API routes; False when there is no build."""
    root = build_dir.resolve()
    index = root / "index.html"
    if not index.is_file():
        return False
    csp = _header_csp(index)

    def respond(file: Path, cache: str) -> Response:
        response = FileResponse(file)
        response.headers["X-Content-Type-Options"] = NOSNIFF
        response.headers["Cache-Control"] = cache
        if csp is not None and file == index:
            response.headers["Content-Security-Policy"] = csp
        return response

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(path: str) -> Response:
        if "\0" in path or path.split("/", 1)[0] in API_SEGMENTS:
            return Response(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            immutable = path.startswith(IMMUTABLE_PREFIX)
            return respond(candidate, IMMUTABLE_CACHE if immutable else NO_CACHE)
        last = path.rsplit("/", 1)[-1]
        if "." in last or path.startswith("_app/"):
            return Response(status_code=404)
        return respond(index, NO_CACHE)

    return True
