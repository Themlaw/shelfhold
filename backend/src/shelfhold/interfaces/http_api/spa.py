"""Serves the statically built SPA (SvelteKit adapter-static, `fallback: index.html`)."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from starlette.requests import Request

from shelfhold.infrastructure.http_headers import IMMUTABLE_CACHE, NO_CACHE, spa_csp
from shelfhold.interfaces.http_api.catalog import error_code
from shelfhold.interfaces.http_api.problems import build_problem_response, language_of

API_SEGMENTS = frozenset({"api", "healthz", "readyz", "openapi.json", "opds", "kosync"})
IMMUTABLE_PREFIX = "_app/immutable/"


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
        segment = path.split("/", 1)[0]
        if segment in API_SEGMENTS:  # an API client expects a problem, not a blank
            request_id = getattr(request.state, "request_id", None)
            # The feeds (OPDS, kosync) carry their own absent-resource answer: a problem with
            # `no-store`, in the language of the request. Mirror it so an unknown path under
            # them is indistinguishable from an absent identifier (no oracle).
            if segment in {"opds", "kosync"}:
                return build_problem_response(
                    error_code("not_found"),
                    request_id=request_id,
                    headers={"Cache-Control": "no-store"},
                    language=language_of(request),
                )
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
