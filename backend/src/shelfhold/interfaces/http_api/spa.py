"""Serves the statically built SPA (SvelteKit adapter-static, `fallback: index.html`)."""

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from starlette.requests import Request

from shelfhold.infrastructure.http_headers import IMMUTABLE_CACHE, NO_CACHE, source_hash
from shelfhold.interfaces.http_api.catalog import error_code
from shelfhold.interfaces.http_api.problems import build_problem_response

API_SEGMENTS = frozenset({"api", "healthz", "readyz", "openapi.json"})
IMMUTABLE_PREFIX = "_app/immutable/"
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


def mount_spa(app: FastAPI, build_dir: Path) -> bool:
    """Register the SPA catch-all after the API routes; False when there is no build."""
    root = build_dir.resolve()
    index = root / "index.html"
    if not index.is_file():
        return False
    csp = spa_csp(index.read_text(encoding="utf-8"))

    def respond(file: Path, cache: str) -> Response:
        response = FileResponse(file)
        response.headers["Cache-Control"] = cache
        if file == index:
            response.headers["Content-Security-Policy"] = csp
        return response

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(path: str, request: Request) -> Response:
        if "\0" in path:
            return Response(status_code=404)
        if path.split("/", 1)[0] in API_SEGMENTS:  # an API client expects a problem, not a blank
            request_id = getattr(request.state, "request_id", None)
            return build_problem_response(error_code("not_found"), request_id=request_id)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            immutable = path.startswith(IMMUTABLE_PREFIX)
            return respond(candidate, IMMUTABLE_CACHE if immutable else NO_CACHE)
        last = path.rsplit("/", 1)[-1]
        if "." in last or path.startswith("_app/"):
            return Response(status_code=404)
        return respond(index, NO_CACHE)

    return True
