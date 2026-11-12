"""`GET /api/v1/search?q=`: the local search of the catalogue (EF-BIB-010), and with `scope=all`
the federated search over the sources that are switched on (EF-SRC-005, EF-UX-011).

Admin only, for now: a child or a member profile may not search until the visibility barrier
(M3 L09) exists, and the family search (M3 L19) is built on it. A member or a child, or a key of
either, is refused (403) before the index is read and before one source is asked: a remote result
has no rating, so for a child it is never shown, and `scope=all` is not her door. The query is
words, never FTS syntax. A federated search is continued with the `next_cursor` it gave, which
only the same person can use, for the same query, for a short while.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Engine

from shelfhold.acquisition.application.federated_search import (
    FederatedEdition,
    FederatedGroup,
    FederatedPage,
    SourceReport,
)
from shelfhold.catalog.public import search_works
from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops import OpsServices

__all__ = ["build_search_router"]

MAX_QUERY = 200
MAX_CURSOR = 128  # a cursor is a short token: a longer one is not ours


class SearchHitOut(BaseModel):
    work_id: str
    title: str
    kind: str


class SearchOut(BaseModel):
    items: list[SearchHitOut]


class EditionOut(BaseModel):
    source_key: str
    remote_id: str
    title: str
    format: str

    @classmethod
    def of(cls, edition: FederatedEdition) -> EditionOut:
        return cls(
            source_key=edition.source_key, remote_id=edition.remote_id, title=edition.title,
            format=edition.format,
        )  # fmt: skip


class ExternalIdOut(BaseModel):
    provider: str
    value: str


class WorkOut(BaseModel):
    """A Work as the sources name it; `appended`: it was shown before, these Editions are new."""

    group_id: str
    title: str
    appended: bool
    editions: list[EditionOut]
    editions_by_format: dict[str, list[EditionOut]]
    external_ids: list[ExternalIdOut]

    @classmethod
    def of(cls, group: FederatedGroup) -> WorkOut:
        return cls(
            group_id=group.group_id, title=group.title, appended=group.appended,
            editions=[EditionOut.of(e) for e in group.editions],
            editions_by_format={
                fmt: [EditionOut.of(e) for e in found]
                for fmt, found in group.editions_by_format.items()
            },
            external_ids=[ExternalIdOut(provider=p, value=v) for p, v in group.external_ids],
        )  # fmt: skip


class TrustLineOut(BaseModel):
    level: Literal["official", "community", "unknown"]
    label: str
    current: bool


class TrustCardOut(BaseModel):
    level: Literal["official", "community", "unknown"]
    lines: list[TrustLineOut]


class SearchedInOut(BaseModel):
    """One line of "Searched in...": a source that was asked, by key and name (never an address)."""

    source_key: str
    name: str
    format: str
    status: Literal["ok", "error"]
    code: str | None
    returned: int
    trust: TrustCardOut | None

    @classmethod
    def of(cls, report: SourceReport) -> SearchedInOut:
        return cls(
            source_key=report.source_key, name=report.name, format=report.format,
            status=report.status,  # type: ignore[arg-type]
            code=report.code, returned=report.returned,
            trust=TrustCardOut(**report.trust) if report.trust else None,  # type: ignore[arg-type]
        )  # fmt: skip


class FederatedOut(BaseModel):
    items: list[WorkOut]
    searched_in: list[SearchedInOut]
    state: Literal["results", "empty", "unavailable", "no_sources"]
    action: Literal["request"] | None  # nothing to show: the one thing to offer is to ask for it
    formats: list[str]
    next_cursor: str | None

    @classmethod
    def of(cls, page: FederatedPage) -> FederatedOut:
        return cls(
            items=[WorkOut.of(g) for g in page.items],
            searched_in=[SearchedInOut.of(r) for r in page.searched_in],
            state=page.state,  # type: ignore[arg-type]
            action=page.action,  # type: ignore[arg-type]
            formats=list(page.formats), next_cursor=page.next_cursor,
        )  # fmt: skip


def require_admin(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
    if principal.role != "admin":
        raise ProblemError("forbidden")
    return principal


def build_search_router(
    get_engine: Callable[[], Engine], get_ops: Callable[[], OpsServices]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["search"])

    @router.get("/search", response_model=SearchOut | FederatedOut)
    def search(
        admin: Annotated[Principal, Depends(require_admin)],
        q: Annotated[str, Query(max_length=MAX_QUERY)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        scope: Literal["local", "all"] = "local",
        cursor: Annotated[str | None, Query(max_length=MAX_CURSOR)] = None,
    ) -> SearchOut | FederatedOut:
        if scope == "all":
            if not q.strip():
                raise ProblemError("validation_failed")
            with refusals():
                page = get_ops().acquire.federated_search(q, owner=admin.user_id, cursor=cursor)
            return FederatedOut.of(page)
        with get_engine().connect() as connection:
            hits = search_works(connection, q, limit=limit, offset=offset)
        return SearchOut(
            items=[SearchHitOut(work_id=h.work_id, title=h.title, kind=h.kind) for h in hits]
        )

    return router
