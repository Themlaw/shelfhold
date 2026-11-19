"""`/api/v1/selections`: saved selections, read as a profile (EF-BIB-017, M5 L21).

A selection is a name and a stored query of a closed grammar (reading state, kind, shelf, language,
recent addition). It is evaluated each time it is read, in the scope of the profile that reads it:
the Works of the page and the count are made of what that profile may see (`catalog.application.
selections`), so a child never gets a hidden Work from it. A selection belongs to the profile that
made it and is read by that profile alone: another profile's selection, an admin's included, is the
same 404 as an identifier nobody ever had. Reads are open to a session and to a key, as the profile
the credential is; writes are a session's. There is no sharing in M5: a body that names an owner is
refused.
"""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator
from sqlalchemy import Engine

from shelfhold.catalog.public import (
    MAX_PAGE,
    QueryRefusedError,
    SelectionFullError,
    SelectionNotFoundError,
    SelectionQuery,
    SelectionRecord,
    Selections,
    SelectionTooSlowError,
    SelectionViewer,
    parse_query,
)
from shelfhold.catalog.public import dump_selection_query as dump_query
from shelfhold.identity.public import Principal, reader_scope
from shelfhold.interfaces.http_api.auth import require_principal, require_session
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.library.public import cover_hash

__all__ = ["build_selections_router"]

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Values = Annotated[list[Annotated[str, StringConstraints(max_length=64)]], Field(max_length=20)]


class QueryIn(BaseModel):
    """The grammar, as the API document says it. The domain checks every value again."""

    model_config = ConfigDict(extra="forbid")

    reading_state: Values | None = None
    kind: Values | None = None
    shelf: Values | None = None
    lang: Values | None = None
    added_within_days: StrictInt | None = None


class SelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    query: QueryIn


class SelectionChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    query: QueryIn | None = None

    @model_validator(mode="after")
    def _something(self) -> "SelectionChange":
        if self.name is None and self.query is None:
            raise ValueError(type(self).__name__)  # nothing to change
        return self


class SelectionOut(BaseModel):
    id: str
    name: str
    query: dict[str, Any]  # the canonical query (empty when the stored text is not the grammar)
    valid: bool
    created_at: datetime
    updated_at: datetime


class SelectionsOut(BaseModel):
    items: list[SelectionOut]


class SelectionWorkOut(BaseModel):
    work_id: str
    title: str
    kind: str
    edition_id: str | None
    cover_hash: str | None


class SelectionDetailOut(SelectionOut):
    items: list[SelectionWorkOut]  # what she is shown now, in title order
    total: int  # of the Works she may see (never more than the cap below)
    capped: bool  # the count stopped at its cap


def _summary(record: SelectionRecord) -> SelectionOut:
    return SelectionOut(
        id=record.id, name=record.name,
        query=json.loads(dump_query(record.query)) if record.query else {},
        valid=record.query is not None, created_at=record.created_at, updated_at=record.updated_at,
    )  # fmt: skip


def _query_of(body: QueryIn) -> SelectionQuery:
    try:
        return parse_query(body.model_dump(exclude_unset=True))
    except QueryRefusedError as refused:
        raise ProblemError(
            "selection.query_refused", extensions={"reason": refused.reason}
        ) from None


def build_selections_router(
    get_engine: Callable[[], Engine], now: Callable[[], datetime]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/selections", tags=["selections"])
    reader = Annotated[Principal, Depends(require_principal)]
    writer = Annotated[Principal, Depends(require_session)]

    def selections() -> Selections:
        return Selections(get_engine(), now)

    def viewer_of(principal: Principal) -> SelectionViewer:
        with get_engine().connect() as connection:
            return SelectionViewer(principal.user_id, reader_scope(connection, principal.user_id))

    @router.get("", response_model=SelectionsOut)
    def list_selections(principal: reader) -> SelectionsOut:
        found = selections().list_for(viewer_of(principal))
        return SelectionsOut(items=[_summary(r) for r in found])

    @router.post("", status_code=201)
    def create_selection(body: SelectionIn, principal: writer) -> dict[str, str]:
        query = _query_of(body.query)
        try:
            made = selections().create(viewer_of(principal), name=body.name, query=query)
        except SelectionFullError:
            raise ProblemError("selection.full") from None
        return {"id": made}

    @router.get("/{selection_id}", response_model=SelectionDetailOut)
    def read_selection(
        selection_id: str,
        principal: reader,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 60,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    ) -> SelectionDetailOut:
        try:
            record, page = selections().page(
                viewer_of(principal), selection_id, limit=limit, offset=offset
            )
        except SelectionNotFoundError:
            raise ProblemError("not_found") from None
        except SelectionTooSlowError:
            raise ProblemError("selection.too_slow") from None
        return SelectionDetailOut(
            **_summary(record).model_dump(),
            items=[
                SelectionWorkOut(
                    work_id=w.work_id, title=w.title, kind=w.kind, edition_id=w.edition_id,
                    cover_hash=cover_hash(w.cover_ref) if w.cover_ref else None,
                )
                for w in page.works
            ],
            total=page.total, capped=page.capped,
        )  # fmt: skip

    @router.put("/{selection_id}", response_model=SelectionOut)
    def update_selection(
        selection_id: str, body: SelectionChange, principal: writer
    ) -> SelectionOut:
        query = _query_of(body.query) if body.query is not None else None
        try:
            changed = selections().update(
                viewer_of(principal), selection_id, name=body.name, query=query
            )
        except SelectionNotFoundError:
            raise ProblemError("not_found") from None
        return _summary(changed)

    @router.delete("/{selection_id}", status_code=204)
    def delete_selection(selection_id: str, principal: writer) -> Response:
        try:
            selections().delete(viewer_of(principal), selection_id)
        except SelectionNotFoundError:
            raise ProblemError("not_found") from None
        return Response(status_code=204)

    return router
