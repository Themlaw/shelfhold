"""`/api/v1/collections`: collections and reading lists, read as a profile (EF-BIB-016, M4 L18a).

A collection is the household's (an admin makes it; every profile reads what of it she may see) or a
profile's own (`scope: profile`: private, read by its owner and by an admin only). Reads are open to
a session and to a key, as the profile the credential is: a key reads what its profile reads. Writes
are a session's: a key, read-only or not, writes nothing here.

A list that is not the reader's is exactly a list that does not exist (the same 404 for another
profile's list and for an identifier nobody ever had). What a reader is shown is made of the Works
she may see (`catalog.application.collections`): the count, the order, the cover and the name never
tell of a hidden Work. An import of a CBL file keeps the order of the file and lists the lines it
could not resolve; a file that is not what it says, or is too big or too deep, or declares an
entity, is a 422 that creates nothing.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import Engine

from shelfhold.catalog.public import (
    MAX_CBL_BYTES,
    CblError,
    CollectionFullError,
    CollectionNotFoundError,
    CollectionNotYoursError,
    Collections,
    CollectionSummary,
    CollectionViewer,
    ItemView,
    parse_cbl,
)
from shelfhold.identity.public import Principal, reader_scope
from shelfhold.interfaces.http_api.auth import require_principal, require_session
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.library.public import cover_hash

__all__ = ["build_collections_router"]

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Scope = Literal["shared", "profile"]


class CollectionOut(BaseModel):
    id: str
    kind: Literal["collection", "reading_list"]
    name: str | None  # null: withheld, it could tell of a Work she may not see
    scope: Scope
    mine: bool
    count: int  # of the items she is shown
    cover_hash: str | None  # the address of the first item shown's thumbnail
    created_at: datetime


class ItemOut(BaseModel):
    id: str
    position: int  # numbered again from 1 among the items she is shown
    work_id: str
    title: str
    number: str | None
    cover_hash: str | None


class CollectionsOut(BaseModel):
    items: list[CollectionOut]


class CollectionDetailOut(CollectionOut):
    items: list[ItemOut]


class CollectionIn(BaseModel):
    name: Name
    kind: Literal["collection", "reading_list"] = "reading_list"
    scope: Scope = "profile"


class ItemIn(BaseModel):
    work_id: Annotated[str, StringConstraints(min_length=26, max_length=26)]
    number: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None


class ImportIn(BaseModel):
    cbl: str = Field(
        max_length=MAX_CBL_BYTES
    )  # the text of the file; the cap in bytes is the parser's
    scope: Scope = "profile"
    name: Name | None = None


class UnresolvedOut(BaseModel):
    index: int  # the line of the file, from 0
    series: str
    number: str
    reason: Literal["not_found", "ambiguous"]


class ImportOut(BaseModel):
    collection: CollectionOut
    imported: int
    unresolved: list[UnresolvedOut]


def _summary(found: CollectionSummary) -> CollectionOut:
    return CollectionOut(
        id=found.id, kind=found.kind,  # type: ignore[arg-type]
        name=found.name, scope=found.scope, mine=found.mine, count=found.count,
        cover_hash=cover_hash(found.cover_ref) if found.cover_ref else None,
        created_at=found.created_at,
    )  # fmt: skip


def _item(found: ItemView) -> ItemOut:
    return ItemOut(
        id=found.id, position=found.position, work_id=found.work_id, title=found.title,
        number=found.number,
        cover_hash=cover_hash(found.cover_ref) if found.cover_ref else None,
    )  # fmt: skip


def build_collections_router(get_engine: Callable[[], Engine]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/collections", tags=["collections"])
    reader = Annotated[Principal, Depends(require_principal)]
    writer = Annotated[Principal, Depends(require_session)]

    def collections() -> Collections:
        return Collections(get_engine())

    def viewer_of(principal: Principal) -> CollectionViewer:
        with get_engine().connect() as connection:
            scope = reader_scope(connection, principal.user_id)
        return CollectionViewer(principal.user_id, principal.role == "admin", scope)

    @router.get("", response_model=CollectionsOut)
    def list_collections(
        principal: reader,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> CollectionsOut:
        found = collections().list_for(viewer_of(principal), limit=limit, offset=offset)
        return CollectionsOut(items=[_summary(c) for c in found])

    @router.post("", status_code=201)
    def create_collection(body: CollectionIn, principal: writer) -> dict[str, str]:
        try:
            made = collections().create(
                viewer_of(principal), name=body.name, kind=body.kind, scope=body.scope
            )
        except CollectionNotYoursError:
            raise ProblemError("forbidden") from None
        return {"id": made}

    @router.post("/import-cbl", status_code=201, response_model=ImportOut)
    def import_cbl(body: ImportIn, principal: writer) -> ImportOut:
        viewer = viewer_of(principal)
        if body.scope == "shared" and not viewer.is_admin:
            raise ProblemError("forbidden")  # before the file is read
        try:
            parsed = parse_cbl(body.cbl.encode("utf-8"))
        except CblError as refused:
            raise ProblemError(
                "collection.cbl_refused", extensions={"reason": refused.reason}
            ) from None
        done = collections().import_list(viewer, parsed, scope=body.scope, name=body.name)
        return ImportOut(
            collection=_summary(done.collection), imported=done.imported,
            unresolved=[
                UnresolvedOut(index=u.index, series=u.series, number=u.number, reason=u.reason)
                for u in done.unresolved
            ],
        )  # fmt: skip

    @router.get("/{collection_id}", response_model=CollectionDetailOut)
    def read_collection(collection_id: str, principal: reader) -> CollectionDetailOut:
        try:
            view = collections().get(viewer_of(principal), collection_id)
        except CollectionNotFoundError:
            raise ProblemError("not_found") from None
        return CollectionDetailOut(
            **_summary(view.summary).model_dump(), items=[_item(i) for i in view.items]
        )

    @router.delete("/{collection_id}", status_code=204)
    def delete_collection(collection_id: str, principal: writer) -> Response:
        try:
            collections().delete(viewer_of(principal), collection_id)
        except CollectionNotFoundError:
            raise ProblemError("not_found") from None
        except CollectionNotYoursError:
            raise ProblemError("forbidden") from None
        return Response(status_code=204)

    @router.post("/{collection_id}/items", status_code=201, response_model=ItemOut)
    def add_item(collection_id: str, body: ItemIn, principal: writer) -> ItemOut:
        try:
            made = collections().add_item(
                viewer_of(principal), collection_id, body.work_id, body.number
            )
        except CollectionNotFoundError:
            raise ProblemError("not_found") from None
        except CollectionNotYoursError:
            raise ProblemError("forbidden") from None
        except CollectionFullError:
            raise ProblemError("collection.full") from None
        return _item(made)

    @router.delete("/{collection_id}/items/{item_id}", status_code=204)
    def remove_item(collection_id: str, item_id: str, principal: writer) -> Response:
        try:
            collections().remove_item(viewer_of(principal), collection_id, item_id)
        except CollectionNotFoundError:
            raise ProblemError("not_found") from None
        except CollectionNotYoursError:
            raise ProblemError("forbidden") from None
        return Response(status_code=204)

    return router
