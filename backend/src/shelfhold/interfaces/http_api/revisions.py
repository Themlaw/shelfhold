"""`/api/v1/revisions`: the revisions recorded without replacing a file (EF-BIB-043, EF-BIB-024).

Listing is for an admin. Applying is a reserved action (a fresh session, never a key): the
metadata-only revisions go to Kavita by its API, and the "to rewrite" queue is asked for only with
the token of the batch (409 `revisions.confirmation_required` otherwise). A revision the
suspect-revision guard quarantined is applied only through `/revisions/force`, a reserved action
of its own: there is no flag on `/revisions/apply` that lifts the guard.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices

__all__ = ["build_revisions_router"]

_APPLY = reserved_action(RESERVED_ACTIONS["revisions.apply"])
_FORCE = reserved_action(RESERVED_ACTIONS["revisions.force"])
Admin = Annotated[Principal, Depends(require_admin)]


class RevisionOut(BaseModel):
    id: str
    unit_id: str
    variant_id: str
    revision: int
    nature: str
    suspect: str | None
    field: str | None
    read: bool
    koreader_reset: bool


class RevisionsOut(BaseModel):
    items: list[RevisionOut]
    koreader_positions: int  # files already read whose position would be reset
    token: str  # typed back to confirm the batch
    newly_queued: int


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # no `force` here: it has its own route

    confirm: str | None = None
    include_read: bool = False


class ForceIn(ApplyIn):
    revision_ids: list[str] = Field(min_length=1)  # the quarantined revisions named, never a batch


class AppliedOut(BaseModel):
    metadata_applied: int
    requested: int
    skipped_read: int
    skipped_suspect: int


def build_revisions_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["revisions"])

    @router.get("/revisions")
    def revisions_list(_admin: Admin) -> RevisionsOut:
        with refusals():
            return RevisionsOut(**get_ops().revisions.list_pending())

    def _apply(body: ApplyIn, principal: Principal, *, force: tuple[str, ...]) -> AppliedOut:
        with refusals():
            done = get_ops().revisions.apply(
                confirm=body.confirm, include_read=body.include_read, force_ids=force,
                principal=principal,
            )  # fmt: skip
        return AppliedOut(**done)

    @router.post("/revisions/apply")
    def revisions_apply(
        body: ApplyIn, principal: Annotated[Principal, Depends(_APPLY)]
    ) -> AppliedOut:
        return _apply(body, principal, force=())

    @router.post("/revisions/force")
    def revisions_force(
        body: ForceIn, principal: Annotated[Principal, Depends(_FORCE)]
    ) -> AppliedOut:
        return _apply(body, principal, force=tuple(body.revision_ids))

    return router
