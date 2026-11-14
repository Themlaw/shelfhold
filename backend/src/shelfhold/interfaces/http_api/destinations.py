"""`/api/v1/destinations` and the detachment doors of L45 (EF-EXP-031, EF-EXP-002).

The screens of L45 stand on these five doors: the list of the destinations with their naming
template, the preview and the removal of a destination, the preview and the full detachment of a
Kavita target. The two that act are reserved (`destination.remove_all`, `detach.full`): a fresh
admin session or the host CLI, never a key, a member or a child.
"""

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import require_session, reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops import OpsServices

__all__ = ["build_destinations_router"]

_REMOVE_ALL = reserved_action(RESERVED_ACTIONS["destination.remove_all"])
_DETACH_FULL = reserved_action(RESERVED_ACTIONS["detach.full"])


def _admin_session(request: Request) -> Principal:
    principal = require_session(request)
    if principal.role != "admin":
        raise ProblemError("forbidden")
    return principal


AdminSession = Depends(_admin_session)


class RemovalPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["keep_files", "delete_files"]


class RemovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["keep_files", "delete_files"]
    typed: str = Field(min_length=1, repr=False)  # names a folder: never in a trace
    give_up: bool = False


class DetachIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    typed: str = Field(min_length=1, repr=False)  # names a folder: never in a trace
    credential: str | None = Field(default=None, repr=False)  # the Kavita password
    discard_hidden_data: bool = False
    discard_tombstone_journal: bool = False
    key_gone: bool = False
    give_up: bool = False


def build_destinations_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["destinations"])

    @router.get("/destinations")
    def list_destinations(_principal: Principal = AdminSession) -> dict[str, Any]:
        with refusals():
            items = get_ops().teardown.list_destinations()
        return {"items": items}

    @router.post("/destinations/{destination_id}/removal-preview")
    def removal_preview(
        destination_id: str,
        body: RemovalPreviewIn,
        _principal: Principal = Depends(_REMOVE_ALL),  # noqa: B008
    ) -> dict[str, Any]:
        with refusals():
            return get_ops().teardown.remove_preview(destination_id=destination_id, mode=body.mode)

    @router.post("/destinations/{destination_id}/removal")
    def removal(
        destination_id: str,
        body: RemovalIn,
        principal: Principal = Depends(_REMOVE_ALL),  # noqa: B008
    ) -> dict[str, Any]:
        with refusals():
            return get_ops().teardown.remove(
                destination_id=destination_id,
                mode=body.mode,
                typed=body.typed,
                give_up=body.give_up,
                principal=principal,
            )

    @router.post("/targets/{target_id}/detach-preview")
    def detach_preview(
        target_id: str,
        _principal: Principal = Depends(_DETACH_FULL),  # noqa: B008
    ) -> dict[str, Any]:
        with refusals():
            return get_ops().teardown.full_preview(target_id=target_id)

    @router.post("/targets/{target_id}/detach")
    def detach(
        target_id: str,
        body: DetachIn,
        principal: Principal = Depends(_DETACH_FULL),  # noqa: B008
    ) -> dict[str, Any]:
        with refusals():
            return get_ops().teardown.full(
                target_id=target_id,
                typed=body.typed,
                credential=body.credential,
                discard_hidden_data=body.discard_hidden_data,
                discard_tombstone_journal=body.discard_tombstone_journal,
                key_gone=body.key_gone,
                give_up=body.give_up,
                principal=principal,
            )

    return router
