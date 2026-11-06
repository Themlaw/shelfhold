"""`/api/v1/intents`: what an interrupted file operation left open, and its settlement (EF-INT-014).

Listing is for an admin (a session or a key with the `admin` scope). Settling an intent acts on the
library: it is the reserved action `intents.resolve`, a fresh session and never a key, and its
answer is never kept for an idempotent replay.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices

__all__ = ["build_intents_router"]

_RESOLVE = reserved_action(RESERVED_ACTIONS["intents.resolve"])
Admin = Annotated[Principal, Depends(require_admin)]


class IntentOut(BaseModel):
    id: str
    op: str
    state: str
    src: str | None
    dst: str | None
    plan_id: str | None
    epoch: int
    created_at: str | None
    stale_from_backup: bool


class IntentsOut(BaseModel):
    items: list[IntentOut]


class ResolveIn(BaseModel):
    action: Literal["replay", "abandon"] = "replay"
    confirm: str | None = None  # `abandon`: the identifier of the intent, typed again


class ResolvedOut(BaseModel):
    intent_id: str
    outcome: str  # completed | replayed | stale_from_backup | cancelled


def build_intents_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["intents"])

    @router.get("/intents")
    def intents_list(_admin: Admin) -> IntentsOut:
        with refusals():
            return IntentsOut(items=[IntentOut(**i) for i in get_ops().intents.list_open()])

    @router.post("/intents/{intent_id}/resolve")
    def intents_resolve(
        intent_id: str, body: ResolveIn, principal: Annotated[Principal, Depends(_RESOLVE)]
    ) -> ResolvedOut:
        with refusals():
            done = get_ops().intents.resolve(
                intent_id, action=body.action, confirm=body.confirm, principal=principal
            )
        return ResolvedOut(**done)

    return router
