"""`/api/v1/sources/{key}/search`, `/api/v1/sources/{key}/filters`, `/api/v1/acquire` (EF-SRC-015,
EF-SRC-004): look for a book in a source and ask for it, or paste its URL.

`search` and `filters` only read. `POST /acquire` takes a URL (the first active source that
recognises it, 422 `source.no_match` otherwise) or a source and a series; it answers `202` with the
job of the `request` class that downloads the file, or `200` when the file is already in the
library. Admin only; a session needs its CSRF token like every state-changing route. Not reserved
actions: they ask for a file, they delete nothing.
"""

import json
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.accepted import JobAccepted, accepted_job
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices, user_of

__all__ = ["build_sources_router"]

Admin = Annotated[Principal, Depends(require_admin)]


class SeriesOut(BaseModel):
    source_key: str
    remote_id: str
    title: str


class SearchOut(BaseModel):
    items: list[SeriesOut]
    next: str | None


class AcquireIn(BaseModel):
    url: str | None = None
    source_key: str | None = None
    remote_id: str | None = None
    unit: str | None = None


class StoredOut(BaseModel):
    state: str


def build_sources_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["sources"])

    @router.get("/sources/{source_key}/filters")
    def filters(source_key: str, _admin: Admin) -> dict[str, Any]:
        with refusals():
            return {"filters": get_ops().acquire.filters(source_key)}

    @router.get("/sources/{source_key}/search")
    def search(
        source_key: str,
        _admin: Admin,
        q: Annotated[str, Query(max_length=200)] = "",
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        filters: Annotated[str | None, Query(max_length=2000)] = None,
    ) -> SearchOut:
        try:
            wanted = json.loads(filters) if filters else None
        except ValueError:
            wanted = None
        if filters and not isinstance(wanted, dict):
            raise ProblemError("source.filter_invalid")
        with refusals():
            return SearchOut(
                **get_ops().acquire.search(source_key, q, filters=wanted, cursor=cursor)
            )

    @router.post("/acquire", status_code=202, response_model=JobAccepted | StoredOut)
    def acquire(body: AcquireIn, admin: Admin) -> JSONResponse:
        with refusals():
            answer = get_ops().acquire.acquire(
                url=body.url, source_key=body.source_key, remote_id=body.remote_id,
                unit=body.unit, requester_user_id=user_of(admin),
            )  # fmt: skip
        if answer["job_id"] is None:
            return JSONResponse(StoredOut(state=answer["state"]).model_dump(), status_code=200)
        return accepted_job(answer["job_id"])

    return router
