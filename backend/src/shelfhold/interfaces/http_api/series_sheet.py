"""The series sheet (M3 L43): `GET /editions/{id}/sheet` and the settings written from it.

The sheet is the admin's (403 for anyone else, by session and by key). It tells which actions the
server allows (`can`), and the screen shows no other. The writes are the admin's plain settings
(the CSRF of a session applies): the order of the bindings, the upstream status set by hand and
the preferred team. The plan of a suggested attach and its confirmation are the reserved action
`rate` (a fresh session, never a key): the same `AttachOps` as `edition attach-confirm`, so the
screen opens no second door. Every refusal is a problem of the catalog, never a 500.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from shelfhold.identity.domain.catalogue import RESERVED_ACTIONS
from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices

__all__ = ["build_series_sheet_router"]

Admin = Annotated[Principal, Depends(require_admin)]
_ATTACH = reserved_action(RESERVED_ACTIONS["rate"])
UpstreamStatus = Literal["ongoing", "finished", "paused", "unknown"]


class SheetBindingOut(BaseModel):
    id: str
    source_key: str
    rank: int
    active: bool
    disabled_reason: Literal["not_loaded", "content_failures", "removed_upstream", "manual"] | None
    last_checked: datetime | None
    next_expected: datetime | None


class SheetGapOut(BaseModel):
    track: str
    segment: str
    number: str


class SheetTeamOut(BaseModel):
    id: str
    label: str
    rank: int | None
    preferred: bool


class SheetFieldOut(BaseModel):
    on: Literal["edition", "work"]
    field: str
    lang: str
    value: str
    manual: bool
    locked: bool


class SheetMergeOut(BaseModel):
    merge_id: str
    merged_at: datetime
    undoable_until: datetime


class SheetAttachOut(BaseModel):
    attach_id: str
    from_work_id: str
    to_work_id: str


class SheetCanOut(BaseModel):
    check: bool
    reorder: bool
    acknowledge_gap: bool
    set_upstream_status: bool
    prefer_team: bool
    edit_field: bool
    separate: bool
    confirm_attach: bool


class SheetOut(BaseModel):
    edition_id: str
    work_id: str
    title: str
    lang: str
    shelf_id: str | None
    cover_hash: str | None
    upstream_status: UpstreamStatus
    upstream_status_by: str | None
    followed: bool
    bindings: list[SheetBindingOut]
    gaps: list[SheetGapOut]
    teams: list[SheetTeamOut]
    fields: list[SheetFieldOut]
    merges: list[SheetMergeOut]
    attach: SheetAttachOut | None
    can: SheetCanOut


class BindingOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_ids: list[str] = Field(min_length=1, max_length=64)


class BindingOrderOut(BaseModel):
    edition_id: str
    order: list[str]


class UpstreamStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: UpstreamStatus


class UpstreamStatusOut(BaseModel):
    edition_id: str
    upstream_status: UpstreamStatus
    by: str


class PreferredTeamIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: str


class PreferredTeamOut(BaseModel):
    edition_id: str
    team_id: str


class AttachWithdrawalOut(BaseModel):
    edition_id: str
    shelf_id: str
    reason: str


class AttachPlanOut(BaseModel):
    attach_id: str
    edition_id: str
    work_id: str
    token: str
    withdrawals: list[AttachWithdrawalOut]


class AttachConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(min_length=1, max_length=256)  # the token of the plan the admin read


def build_series_sheet_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["series-sheet"])

    @router.get("/editions/{edition_id}/sheet")
    def edition_sheet(edition_id: str, _admin: Admin) -> SheetOut:
        with refusals():
            return SheetOut(**get_ops().sheet.sheet(edition_id))

    @router.post("/editions/{edition_id}/bindings/order")
    def bindings_order(edition_id: str, body: BindingOrderIn, _admin: Admin) -> BindingOrderOut:
        with refusals():
            return BindingOrderOut(**get_ops().sheet.reorder(edition_id, body.binding_ids))

    @router.post("/editions/{edition_id}/upstream-status")
    def upstream_status_set(edition_id: str, body: UpstreamStatusIn, _admin: Admin) -> UpstreamStatusOut:
        with refusals():
            return UpstreamStatusOut(**get_ops().sheet.set_upstream_status(edition_id, body.status))

    @router.post("/editions/{edition_id}/preferred-team")
    def preferred_team_set(edition_id: str, body: PreferredTeamIn, _admin: Admin) -> PreferredTeamOut:
        with refusals():
            return PreferredTeamOut(**get_ops().sheet.prefer_team(edition_id, body.team_id))

    @router.post("/attachments/{attach_id}/plan")
    def attach_plan(
        attach_id: str, _principal: Annotated[Principal, Depends(_ATTACH)]
    ) -> AttachPlanOut:
        with refusals():
            return AttachPlanOut(**get_ops().attach.plan(attach_id))

    @router.post("/attachments/{attach_id}/confirm")
    def attach_confirm(
        attach_id: str,
        body: AttachConfirmIn,
        principal: Annotated[Principal, Depends(_ATTACH)],
    ) -> AttachPlanOut:
        with refusals():
            done = get_ops().attach.confirm(attach_id, confirm=body.confirm, principal=principal)
        return AttachPlanOut(**done)

    return router
