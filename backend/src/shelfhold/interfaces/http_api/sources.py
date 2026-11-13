"""`/api/v1/sources/{key}/search`, `/api/v1/sources/{key}/filters`, `/api/v1/acquire` (EF-SRC-015,
EF-SRC-004): look for a book in a source and ask for it, or paste its URL.

`search` and `filters` only read. `POST /acquire` takes a URL (the first active source that
recognises it, 422 `source.no_match` otherwise) or a source and a series; it answers `202` with the
job of the `request` class that downloads the file, or `200` when the file is already in the
library. Admin only; a session needs its CSRF token like every state-changing route. Not reserved
actions: they ask for a file, they delete nothing.

`POST /sources/{source_key}/migrate-ids` (M3 L27, EF-SRC-022) is the one that is reserved: the bulk
migration of the identity of a source's series, the twin of `source migrate-ids`. A fresh admin
session, never a key, a member or a child; the preview (no `confirm`) opens the tombstones and
writes nothing, `confirm` is the fingerprint of a preview and applies exactly that plan.
"""

import json
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.accepted import JobAccepted, accepted_job
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices, user_of

__all__ = ["build_sources_router"]

Admin = Annotated[Principal, Depends(require_admin)]
# the guard writes no line (`audit=False`): the ops layer writes it, in the transaction that moves
# the rows; a preview or a refusal leaves none
_MIGRATE_IDS = reserved_action(RESERVED_ACTIONS["source.migrate_ids"], audit=False)


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


class MigrateIdsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_scheme: str = Field(min_length=1, max_length=64)
    to_scheme: str = Field(min_length=1, max_length=64)
    confirm: str | None = Field(
        default=None, min_length=1, max_length=128
    )  # a preview's fingerprint

    @model_validator(mode="after")
    def _schemes_differ(self) -> "MigrateIdsIn":
        if self.from_scheme == self.to_scheme:
            raise ValueError("schemes_must_differ")
        return self


class MigrationRowOut(BaseModel):
    id: str
    old: str
    new: str  # empty for a series a tombstone holds: where it would have gone is never said
    reason: str


class MigrateIdsOut(BaseModel):
    source_key: str
    from_scheme: str
    to_scheme: str
    migrate: list[MigrationRowOut]
    blocked: list[MigrationRowOut]
    unresolved: list[MigrationRowOut]
    fingerprint: str
    applied: int | None  # None for a preview


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

    @router.post("/sources/{source_key}/migrate-ids")
    def migrate_ids(
        source_key: str,
        body: MigrateIdsIn,
        principal: Annotated[Principal, Depends(_MIGRATE_IDS)],
    ) -> MigrateIdsOut:
        with refusals():
            done = get_ops().source_migration.migrate_ids(
                source_key, from_scheme=body.from_scheme, to_scheme=body.to_scheme,
                confirm=body.confirm, principal=principal,
            )  # fmt: skip
        return MigrateIdsOut(**done)

    return router
