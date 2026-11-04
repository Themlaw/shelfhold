"""Serves the statically built SPA (SvelteKit adapter-static, `fallback: index.html`)."""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

API_PREFIXES = ("api/", "healthz", "readyz", "openapi.json")


def mount_spa(app: FastAPI, build_dir: Path) -> bool:
    """Register the SPA catch-all after the API routes; False when there is no build."""
    root = build_dir.resolve()
    index = root / "index.html"
    if not index.is_file():
        return False

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        if path.startswith(API_PREFIXES):
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, media_type="text/html")

    return True
