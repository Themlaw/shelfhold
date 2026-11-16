"""`/api/v1/works/{work_id}/relations`: related Works (EF-BIB-039, M4 L19).

A relation links two Works that stay two (a novel and its adaptation, a sequel, a spin-off): never a
merge, never an Edition moved. Making and removing one is the admin's, as `plans`: an admin session,
or a key of scope `admin` of the admin (a member, a child, and a read or request key are refused
before anything is looked at, so the refusal never tells which Works exist). Reading is every
profile's, a key as the profile it is.

What a reader is shown is made of the Works she may see and of nothing else: a relation whose other
end she may not see is not in the list and not in the count, and the Work whose relations she asks
for, if she may not see it, is exactly a Work that does not exist (the same 404, the same body).
An old identifier of a merged Work leads to the Work that absorbed it.
"""

import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, StringConstraints
from sqlalchemy import Engine

from shelfhold.catalog.public import (
    RelationCycleError,
    RelationError,
    RelationNotFoundError,
    Relations,
)
from shelfhold.identity.public import Principal, reader_scope
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.plans import require_admin
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.library.public import cover_hash

__all__ = ["build_relations_router"]

Ulid = Annotated[str, StringConstraints(min_length=26, max_length=26)]
Kind = Annotated[str, StringConstraints(pattern=re.compile(r"^[a-z0-9_]{1,40}$"))]


class RelationOut(BaseModel):
    work_id: str  # the other Work
    title: str
    kind: str
    outgoing: bool  # True: this Work is `kind` of the other one (from this, to the other)
    edition_id: str | None  # the other Work's first Edition she may see: where its page is
    cover_hash: str | None


class RelationsOut(BaseModel):
    work_id: str
    count: int  # of the relations she is shown
    items: list[RelationOut]


class RelationIn(BaseModel):
    to_work_id: Ulid  # this Work is `kind` of this one
    kind: Kind


class RelationCreatedOut(BaseModel):
    created: bool  # False: the same relation already existed, nothing changed


def build_relations_router(
    get_engine: Callable[[], Engine], now: Callable[[], datetime]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/works", tags=["relations"])
    reader = Annotated[Principal, Depends(require_principal)]
    admin = Annotated[Principal, Depends(require_admin)]

    def relations() -> Relations:
        return Relations(get_engine(), now)

    @router.get("/{work_id}/relations", response_model=RelationsOut)
    def relations_read(work_id: str, principal: reader) -> RelationsOut:
        with get_engine().connect() as connection:
            scope = reader_scope(connection, principal.user_id)
        found = relations().shown(scope, principal.user_id, work_id)
        if found is None:
            raise ProblemError("not_found")
        resolved, shown = found
        return RelationsOut(
            work_id=resolved, count=len(shown),
            items=[
                RelationOut(
                    work_id=r.work_id, title=r.title, kind=r.kind, outgoing=r.outgoing,
                    edition_id=r.edition_id,
                    cover_hash=cover_hash(r.cover_ref) if r.cover_ref else None,
                )
                for r in shown
            ],
        )  # fmt: skip

    @router.post("/{work_id}/relations", status_code=201, response_model=RelationCreatedOut)
    def relations_add(
        work_id: str, body: RelationIn, response: Response, _admin: admin
    ) -> RelationCreatedOut:
        try:
            created = relations().add(work_id, body.to_work_id, body.kind)
        except RelationNotFoundError:
            raise ProblemError("not_found") from None
        except RelationCycleError:
            raise ProblemError("relation.cycle") from None
        except RelationError:
            raise ProblemError("relation.invalid") from None
        if not created:
            response.status_code = 200
        return RelationCreatedOut(created=created)

    @router.delete("/{work_id}/relations/{kind}/{to_work_id}", status_code=204)
    def relations_remove(work_id: str, kind: str, to_work_id: str, _admin: admin) -> Response:
        if not relations().remove(work_id, to_work_id, kind):
            raise ProblemError("not_found")
        return Response(status_code=204)

    return router
