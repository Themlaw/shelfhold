"""`GET /api/v1/about` (ENF-LEG-012) and `GET /api/v1/openapi.json` (A6).

`/about` is for any principal and lists no action; the schema, which names every route, is the
admin's."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.stats import require_admin

SOURCE_REPOSITORY = "https://github.com/Themlaw/shelfhold"


def default_source_url(version: str) -> str:
    """The repository at the tag of the running version; a fork sets `source_url` instead."""
    return f"{SOURCE_REPOSITORY}/tree/v{version}"


class About(BaseModel):
    version: str
    source_url: str


def build_about_router(*, version: str, source_url: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/about", response_model=About)
    def about(_principal: Annotated[Principal, Depends(require_principal)]) -> About:
        return About(version=version, source_url=source_url)

    @router.get("/openapi.json", include_in_schema=False)
    def openapi(
        request: Request, _admin: Annotated[Principal, Depends(require_admin)]
    ) -> dict[str, Any]:
        schema: dict[str, Any] = request.app.openapi()
        return schema

    return router
