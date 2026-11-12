"""`GET /api/v1/works` and `GET /api/v1/shelves`, with `?since=` removals (M3 L11, EF-INT-001).

The list of Works is the reader's own view (`reader_scope`, the one filter): a child, by session or
by key, is shown what her ceiling lets through and nothing else. `?since=` brings back what left
that view since the token (see `catalog.application.changes`): never a Work she did not see when
the token was issued, never a title (only ids). A token is opaque and signed (`lists.py`); a forged
one is `invalid_cursor`, one older than the event retention (or newer than the head) is
`resync_required`, and so are more removals than one answer carries.

The list of shelves is the admin's (a child, a member, a key of theirs: 403, before the token is
read): shelves are household settings, and what they hold is never an age.

The answer carries `since`, the token to send next time, read in the same transaction as the page.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import (
    TooManyRemovalsError,
    count_visible_works,
    list_visible_works,
    removed_ids,
    window,
)
from shelfhold.events.since_key import SinceKey
from shelfhold.export.application.shelves import list_shelves
from shelfhold.identity.public import Principal, reader_scope
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.lists import (
    decode_since,
    encode_since,
    since_param,
)
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.stats import require_admin

__all__ = ["build_catalog_lists_router"]

WORKS_KIND = "works"
SHELVES_KIND = "shelves"


class WorkItemOut(BaseModel):
    work_id: str
    title: str
    kind: str


class WorksPageOut(BaseModel):
    items: list[WorkItemOut]
    total: int
    since: str  # the token to send next time
    removed: list[str]  # Work ids that left this reader's view since the token (empty without one)


class ShelfItemOut(BaseModel):
    id: str
    name: str
    max_age_rating: int | None


class ShelvesPageOut(BaseModel):
    items: list[ShelfItemOut]
    since: str
    removed: list[str]


def _removals(
    connection: Connection,
    raw: str | None,
    key: SinceKey,
    kind: str,
    read: Callable[[int], list[str]],
) -> tuple[str, list[str]]:
    """The next token and the removals after `raw`, both read at the head of one snapshot."""
    seen = window(connection)
    next_token = encode_since(seen.head, key=key.value(), kind=kind)
    if raw is None:
        return next_token, []
    since = decode_since(raw, key=key.value(), kind=kind)
    since.require_not_older_than(seen.floor)  # the events after the token were purged
    if since.seq > seen.head:  # a token this instance has not issued yet: a restored backup
        raise ProblemError("resync_required")
    try:
        return next_token, read(since.seq)
    except TooManyRemovalsError:
        raise ProblemError("resync_required") from None


def build_catalog_lists_router(get_engine: Callable[[], Engine], key: SinceKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["catalog"])

    @router.get("/works", response_model=WorksPageOut)
    def list_works(
        principal: Annotated[Principal, Depends(require_principal)],
        raw: Annotated[str | None, Depends(since_param)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> WorksPageOut:
        with get_engine().connect() as connection:
            scope = reader_scope(connection, principal.user_id)
            token, removed = _removals(
                connection, raw, key, WORKS_KIND,
                lambda seq: removed_ids(connection, scope, seq),
            )  # fmt: skip
            page = list_visible_works(connection, scope, limit=limit, offset=offset)
            total = count_visible_works(connection, scope)
        return WorksPageOut(
            items=[WorkItemOut(work_id=w.work_id, title=w.title, kind=w.kind) for w in page],
            total=total, since=token, removed=removed,
        )  # fmt: skip

    @router.get("/shelves", response_model=ShelvesPageOut)
    def shelves(
        _admin: Annotated[Principal, Depends(require_admin)],
        raw: Annotated[str | None, Depends(since_param)],
    ) -> ShelvesPageOut:
        with get_engine().connect() as connection:
            token, removed = _removals(
                connection, raw, key, SHELVES_KIND,
                lambda seq: removed_ids(connection, None, seq, kind="shelf"),
            )  # fmt: skip
            found = list_shelves(connection)
        return ShelvesPageOut(
            items=[ShelfItemOut(id=s.id, name=s.name, max_age_rating=s.max_age_rating)
                   for s in found],
            since=token, removed=removed,
        )  # fmt: skip

    return router
