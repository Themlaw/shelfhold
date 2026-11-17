"""`/api/v1/destinations` and the detachment doors of L45 (EF-EXP-031, EF-EXP-002).

The screens of L45 stand on these five doors: the list of the destinations with their naming
template, the preview and the removal of a destination, the preview and the full detachment of a
Kavita target. The two that act are reserved (`destination.remove_all`, `detach.full`): a fresh
admin session or the host CLI, never a key, a member or a child.

Since M4 L34 a removal that takes the files and a full detachment are background jobs: the door
checks the session, the phrase and the choices, writes its audit line, and answers `202 Accepted`
with the `job_id` (and the `Location` of the job), like `export.run`. What the queue keeps is a key
of identifiers; the password of the Kavita account is handed to the job in memory, never stored.
A removal that keeps the files and the `give_up` of an interrupted one touch the database only and
answer `200` with their report. The keyed POST of these routes is never stored: the digest of a
body that holds a phrase or a password is not kept anywhere.
"""

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.accepted import JobAccepted, accepted_job
from shelfhold.interfaces.http_api.auth import require_session, reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops import OpsServices

__all__ = ["build_destinations_router"]

# The service writes its own audit line, with its target, when it really removes or detaches: the
# guard writes none (a preview or a refused phrase deletes nothing).
_REMOVE_ALL = reserved_action(RESERVED_ACTIONS["destination.remove_all"], audit=False)
_DETACH_FULL = reserved_action(RESERVED_ACTIONS["detach.full"], audit=False)


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

    @router.post(
        "/destinations/{destination_id}/removal",
        responses={202: {"model": JobAccepted}},
    )
    def removal(
        destination_id: str,
        body: RemovalIn,
        principal: Principal = Depends(_REMOVE_ALL),  # noqa: B008
    ) -> Any:
        with refusals():
            ops = get_ops().teardown
            if body.mode == "delete_files" and not body.give_up:  # the files: a job (M4 L34)
                job_id = ops.remove_queue(
                    destination_id=destination_id, mode=body.mode, typed=body.typed,
                    principal=principal,
                )  # fmt: skip
                return accepted_job(job_id)
            return ops.remove(
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

    @router.post(
        "/targets/{target_id}/detach",
        responses={202: {"model": JobAccepted}},
    )
    def detach(
        target_id: str,
        body: DetachIn,
        principal: Principal = Depends(_DETACH_FULL),  # noqa: B008
    ) -> Any:
        with refusals():
            ops = get_ops().teardown
            if not body.give_up:  # the detachment: a job (M4 L34)
                job_id = ops.full_queue(
                    target_id=target_id, typed=body.typed, credential=body.credential,
                    discard_hidden_data=body.discard_hidden_data,
                    discard_tombstone_journal=body.discard_tombstone_journal,
                    key_gone=body.key_gone, principal=principal,
                )  # fmt: skip
                return accepted_job(job_id)
            return ops.full(
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
