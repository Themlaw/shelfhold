"""Serves the statically built SPA (SvelteKit adapter-static, `fallback: index.html`)."""

from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from starlette.requests import Request

from shelfhold.infrastructure.http_headers import (
    IMMUTABLE_CACHE,
    MANIFEST_TYPE,
    NO_CACHE,
    WORKER_CSP,
    reader_csp,
    spa_csp,
)
from shelfhold.interfaces.http_api.catalog import error_code
from shelfhold.interfaces.http_api.problems import build_problem_response

API_SEGMENTS = frozenset({"api", "healthz", "readyz", "openapi.json"})
IMMUTABLE_PREFIXES = ("_app/immutable/", "reader-entry/assets/")
READER_SEGMENT = "lire"  # the reader is its own page, under its own policy (ADR-0055)
READER_DIR = "reader-entry"
WORKER_FILE = "service-worker.js"


def odd_path(raw: str, decoded: str) -> bool:
    """A request line the SPA never serves as a page: its decoded form holds a `.` or `..` segment,
    an encoded slash (`%2F`) or a double slash. One rule, shared with the service worker
    (`isReserved`), so that the server and the worker never read the same line as two different
    paths: `/lire%2F..` is not the reader for one and a screen of the SPA for the other."""
    if "%2f" in raw.lower() or "//" in raw or "//" in decoded:
        return True
    return any(segment in {".", ".."} for segment in unquote(raw).split("/"))


def mount_spa(app: FastAPI, build_dir: Path) -> bool:
    """Register the SPA catch-all after the API routes; False when there is no build."""
    root = build_dir.resolve()
    index = root / "index.html"
    if not index.is_file():
        return False
    csp = spa_csp(index.read_text(encoding="utf-8"))
    worker = (root / WORKER_FILE).resolve()
    reader = (root / READER_DIR / "reader.html").resolve()  # compared with resolved paths below
    reader_policy = reader_csp(reader.read_text(encoding="utf-8")) if reader.is_file() else None

    def respond(file: Path, cache: str) -> Response:
        response = FileResponse(file)
        response.headers["Cache-Control"] = cache
        if file.suffix == ".webmanifest":
            response.headers["Content-Type"] = MANIFEST_TYPE
        if file == index:
            response.headers["Content-Security-Policy"] = csp
        elif file == reader and reader_policy is not None:
            response.headers["Content-Security-Policy"] = reader_policy
        elif file == worker:
            response.headers["Content-Security-Policy"] = WORKER_CSP
        return response

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(path: str, request: Request) -> Response:
        raw = request.scope.get("raw_path")
        raw_path = raw.decode("latin-1").split("?", 1)[0] if isinstance(raw, bytes) else "/" + path
        if "\0" in path or odd_path(raw_path, "/" + path):
            return Response(status_code=404)  # neutral: not a page, whatever its first segment says
        if path.split("/", 1)[0] in API_SEGMENTS:  # an API client expects a problem, not a blank
            request_id = getattr(request.state, "request_id", None)
            return build_problem_response(error_code("not_found"), request_id=request_id)
        if path.split("/", 1)[0] == READER_SEGMENT:
            # a full navigation to the reader: its own page and policy, never the shell of the SPA
            if reader_policy is None:
                return Response(status_code=404)
            return respond(reader, NO_CACHE)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            immutable = path.startswith(IMMUTABLE_PREFIXES)
            return respond(candidate, IMMUTABLE_CACHE if immutable else NO_CACHE)
        last = path.rsplit("/", 1)[-1]
        if "." in last or path.startswith("_app/"):
            return Response(status_code=404)
        return respond(index, NO_CACHE)

    return True
