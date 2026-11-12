"""`GET /api/v1/search?q=`: the local search of the catalogue (EF-BIB-010).

Admin only, for now: a child or a member profile may not search until the visibility barrier
(M3 L09) exists, and the family search (M3 L19) is built on it. A member or a child, or a key of
either, is refused (403) before the index is read. The query is words, never FTS syntax.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Engine

from shelfhold.catalog.public import search_works
from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_search_router"]

MAX_QUERY = 200


class SearchHitOut(BaseModel):
    work_id: str
    title: str
    kind: str


class SearchOut(BaseModel):
    items: list[SearchHitOut]


def require_admin(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
    if principal.role != "admin":
        raise ProblemError("forbidden")
    return principal


def build_search_router(get_engine: Callable[[], Engine]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["search"])

    @router.get("/search", response_model=SearchOut)
    def search(
        _admin: Annotated[Principal, Depends(require_admin)],
        q: Annotated[str, Query(max_length=MAX_QUERY)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> SearchOut:
        with get_engine().connect() as connection:
            hits = search_works(connection, q, limit=limit, offset=offset)
        return SearchOut(
            items=[SearchHitOut(work_id=h.work_id, title=h.title, kind=h.kind) for h in hits]
        )

    return router
