"""The admin routes of the M2 catalog commands: field locks, renumbered identifiers, the sorted
list of Editions (EF-BIB-008, EF-BIB-003, ENF-I18N-003), the re-cut chapters (EF-BIB-044) and the
display of an export item. Each is the twin of a CLI command and calls the same service.

The locks, the renumbering and the lists are for an admin (a session, or a key with the `admin`
scope). `coverage resplit propose|apply` is the reserved action `coverage.resplit`: a fresh
session, never a key.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices

__all__ = ["build_catalog_admin_router"]

_RESPLIT = reserved_action(RESERVED_ACTIONS["coverage.resplit"])
Admin = Annotated[Principal, Depends(require_admin)]
On = Literal["edition", "work"]


class FieldIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | None = Field(default=None, max_length=4096)  # none: lock the stored value
    lang: str = Field(default="und", max_length=35)
    on: On = "edition"


class UnlockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lang: str = Field(default="und", max_length=35)
    on: On = "edition"


class FieldOut(BaseModel):
    edition_id: str
    field: str
    lang: str
    on: str
    locked: bool
    kept: bool  # the value that was stored stays


class RenumberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    old: str = Field(min_length=1, max_length=512)
    new: str = Field(min_length=1, max_length=512)
    id_scheme: str | None = Field(default=None, max_length=64)


class RenumberOut(BaseModel):
    edition_id: str
    provider: str
    old: str
    new: str
    added: bool


class EditionLineOut(BaseModel):
    edition_id: str
    work_id: str
    lang: str
    title: str
    sort_key: str


class EditionsOut(BaseModel):
    items: list[EditionLineOut]


class ResplitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_unit_id: str = Field(min_length=1, max_length=64)
    child_unit_ids: list[str] = Field(min_length=1, max_length=200)


class ResplitApplyIn(ResplitIn):
    confirm: str = Field(min_length=1, max_length=256)  # the code `propose` gave


class ResplitProposedOut(BaseModel):
    edition_id: str
    parent: str
    children: list[str]
    code: str


class ResplitAppliedOut(BaseModel):
    edition_id: str
    parent: str
    children: list[str]
    added: int


class ExportItemOut(BaseModel):
    id: str
    edition_id: str
    kind: str
    label: str | None
    closed: bool
    units: int
    kept: str | None
    kept_problem: str | None


def build_catalog_admin_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["catalog"])

    @router.get("/editions")
    def editions_list(
        _admin: Admin,
        sort: Literal["title"] = "title",
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> EditionsOut:
        with refusals():
            rows = get_ops().editions.listing(sort=sort, limit=limit, offset=offset)
        return EditionsOut(items=[EditionLineOut(**r) for r in rows])

    @router.post("/editions/{edition_id}/fields/{field}/lock")
    def field_lock(edition_id: str, field: str, body: FieldIn, _admin: Admin) -> FieldOut:
        with refusals():
            done = get_ops().editions.lock(
                edition_id, field, body.value, lang=body.lang, on=body.on
            )
        return FieldOut(**done)

    @router.post("/editions/{edition_id}/fields/{field}/unlock")
    def field_unlock(edition_id: str, field: str, body: UnlockIn, _admin: Admin) -> FieldOut:
        with refusals():
            done = get_ops().editions.unlock(edition_id, field, lang=body.lang, on=body.on)
        return FieldOut(**done)

    @router.post("/editions/{edition_id}/external-ids/renumber")
    def renumber_id(edition_id: str, body: RenumberIn, _admin: Admin) -> RenumberOut:
        with refusals():
            done = get_ops().editions.renumber(
                edition_id, body.provider, body.old, body.new, id_scheme=body.id_scheme
            )
        return RenumberOut(**done)

    @router.post("/coverage/resplit/propose")
    def resplit_propose(
        body: ResplitIn, principal: Annotated[Principal, Depends(_RESPLIT)]
    ) -> ResplitProposedOut:
        with refusals():
            done = get_ops().resplit.propose(
                body.parent_unit_id, body.child_unit_ids, principal=principal
            )
        return ResplitProposedOut(**done)

    @router.post("/coverage/resplit/apply")
    def resplit_apply(
        body: ResplitApplyIn, principal: Annotated[Principal, Depends(_RESPLIT)]
    ) -> ResplitAppliedOut:
        with refusals():
            done = get_ops().resplit.apply(
                body.parent_unit_id, body.child_unit_ids, confirm=body.confirm,
                principal=principal,
            )  # fmt: skip
        return ResplitAppliedOut(**done)

    @router.get("/export-items/{item_id}")
    def export_item(item_id: str, _admin: Admin) -> ExportItemOut:
        with refusals():
            return ExportItemOut(**get_ops().export_items.show(item_id))

    return router
