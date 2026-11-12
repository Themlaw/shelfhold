"""`GET /api/v1/about` (ENF-LEG-012) and `GET /api/v1/openapi.json` (A6).

`/about` is for any principal and lists no action; the schema, which names every route, is the
admin's."""

import os
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.stats import require_admin

SOURCE_REPOSITORY = "https://github.com/Themlaw/shelfhold"


def default_source_url(version: str, *, revision: str | None = None, release: bool = True) -> str:
    """The code that is really served (AGPL section 13): the tag of the version for a release
    build, the commit for any other build (a version number there names code that differs).
    A fork sets `source_url` instead."""
    if not release and revision and re.fullmatch(r"[0-9a-f]{40}", revision):
        return f"{SOURCE_REPOSITORY}/tree/{revision}"
    return f"{SOURCE_REPOSITORY}/tree/v{version}"


def build_source_url(version: str) -> str:
    """`default_source_url` with what the image build recorded (`SHELFHOLD_BUILD_*`)."""
    return default_source_url(
        version,
        revision=os.environ.get("SHELFHOLD_BUILD_REVISION"),
        release=os.environ.get("SHELFHOLD_BUILD_RELEASE", "true") == "true",
    )


class About(BaseModel):
    version: str
    source_url: str


class Source(BaseModel):
    source_url: str


def build_about_router(*, version: str, source_url: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/about", response_model=About)
    def about(_principal: Annotated[Principal, Depends(require_principal)]) -> About:
        return About(version=version, source_url=source_url)

    @router.get("/source", response_model=Source)
    def source() -> Source:
        """Offered to anyone who reaches the instance, signed in or not (AGPL section 13): the
        footer of every screen links to it. The address only, nothing of the instance."""
        return Source(source_url=source_url)

    @router.get("/openapi.json", include_in_schema=False)
    def openapi(
        request: Request, _admin: Annotated[Principal, Depends(require_admin)]
    ) -> dict[str, Any]:
        schema: dict[str, Any] = request.app.openapi()
        return schema

    return router
