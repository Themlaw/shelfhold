"""`GET /api/v1/search?q=`: the local search of the catalogue (EF-BIB-010), and with `scope=all`
the federated search over the sources that are switched on (EF-SRC-005, EF-UX-011).

The local search answers every profile through the one filter of the catalogue (M3 L09): the
reader's scope (an adult sees everything, a child what is rated and under her ceiling) is a
condition of the query, so a hidden Work is neither returned nor counted, and is answered exactly
like a Work that does not exist. A key or a token reads as its own profile, never as who issued it.
`scope=all` is the administrator's (by session or key). A member, or the key of a member or a
child, is refused (403) before one source is asked. A child has her own form of it (M3 L19,
EF-USR-014): the family search, by her session only, over the sources marked "family", where a
remote result is a title and the action "request" and nothing else, and where the rated catalogue
she may see comes through the same filter as the local search. The query is words, never FTS
syntax. A federated search is continued with the `next_cursor` it gave, which only the same person
can use, for the same query, for a short while.
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
from shelfhold.catalog.public import search_page
from shelfhold.identity.public import Credential, Principal, reader_scope
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
    total: int  # the Works the reader may see that match: never more than the pages hold


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


class FamilyItemOut(BaseModel):
    """What a child sees of a remote result: a title and the one action. No cover, no summary, no
    format, no source name: only what a request needs, by opaque reference."""

    title: str
    action: Literal["request"]
    source_key: str
    remote_id: str


class FamilyOut(BaseModel):
    """The family search of a child (EF-USR-014): the sources marked "family" and the rated
    catalogue, with no total, no count and no word on what was dropped or hidden."""

    items: list[FamilyItemOut]
    local: list[SearchHitOut]  # the catalogue she may see, through the one filter of L09
    state: Literal["results", "empty"]
    next_cursor: str | None


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


def build_search_router(
    get_engine: Callable[[], Engine], get_ops: Callable[[], OpsServices]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["search"])

    @router.get("/search", response_model=SearchOut | FederatedOut | FamilyOut)
    def search(
        principal: Annotated[Principal, Depends(require_principal)],
        q: Annotated[str, Query(max_length=MAX_QUERY)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        scope: Literal["local", "all"] = "local",
        cursor: Annotated[str | None, Query(max_length=MAX_CURSOR)] = None,
    ) -> SearchOut | FederatedOut | FamilyOut:
        if scope == "all":
            with get_engine().connect() as connection:
                viewer = reader_scope(connection, principal.user_id)
            if viewer.kind == "ceiling":  # a child: the reduced form, by her session only
                if principal.credential is not Credential.SESSION:
                    raise ProblemError("forbidden")
                return _family(principal, q, limit, offset, cursor)
            if principal.role != "admin" or not viewer.unrestricted:
                raise ProblemError(
                    "forbidden"
                )  # before the query is read, before a source is asked
            if not q.strip():
                raise ProblemError("validation_failed")
            with refusals():
                page = get_ops().acquire.federated_search(q, owner=principal.user_id, cursor=cursor)
            return FederatedOut.of(page)
        with get_engine().connect() as connection:
            found = search_page(
                connection, q, limit=limit, offset=offset,
                scope=reader_scope(connection, principal.user_id),
            )  # fmt: skip
        return SearchOut(
            items=[SearchHitOut(work_id=h.work_id, title=h.title, kind=h.kind) for h in found.hits],
            total=found.total,
        )

    def _family(
        principal: Principal, q: str, limit: int, offset: int, cursor: str | None
    ) -> FamilyOut:
        if not q.strip():
            raise ProblemError("validation_failed")
        with refusals():
            page = get_ops().acquire.family_search(q, owner=principal.user_id, cursor=cursor)
        local: list[SearchHitOut] = []
        if cursor is None:  # the catalogue is the first page only: a cursor walks the sources
            with get_engine().connect() as connection:
                found = search_page(
                    connection, q, limit=limit, offset=offset,
                    scope=reader_scope(connection, principal.user_id),
                )  # fmt: skip
            local = [
                SearchHitOut(work_id=h.work_id, title=h.title, kind=h.kind) for h in found.hits
            ]
        return FamilyOut(
            items=[
                FamilyItemOut(
                    title=r.title, action=r.action, source_key=r.source_key,
                    remote_id=r.remote_id,
                )
                for r in page.items
            ],
            local=local, state=page.state, next_cursor=page.next_cursor,
        )  # fmt: skip

    return router
