"""The routes of the library operations that had a service but no door (L30, L31, L34, L37).

Pairing with the Suwayomi follow, the switch-over report, the repatriation before an unmount (a
dry run first: the apply carries its fingerprint), the detachment, `purge --now` (a reserved
action: a fresh session, never a key) and the write probe. The CLI twin of each calls the same
`LibraryOps`.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices

__all__ = ["build_library_router"]

_PURGE = reserved_action(RESERVED_ACTIONS["purge.now"])
_DELETE = reserved_action(RESERVED_ACTIONS["unit.delete"])
Admin = Annotated[Principal, Depends(require_admin)]
Body = dict[str, Any]


class PairIn(BaseModel):
    export_series_id: str = Field(min_length=1)
    source_key: str = Field(min_length=1)
    remote_id: str = Field(min_length=1)
    follow_from: str | None = None
    confirm_backfill: int | None = Field(default=None, ge=0)


class PairAutoIn(BaseModel):
    min_score: float = Field(default=0.9, ge=0, le=1)
    fingerprint: str | None = None


class RepatriateIn(BaseModel):
    fingerprint: str | None = None  # without it: the dry run
    source: str | None = None


class DetachIn(BaseModel):
    restore_upstream: Literal[True]  # the only detachment that deletes nothing; false is a 422
    shelf_id: str | None = None
    edition_id: str | None = None


class PurgeIn(BaseModel):
    typed: str = Field(min_length=1)


class DeletionPreviewIn(BaseModel):
    level: Literal["unfollow", "remove_destinations", "delete_content"]
    unit_ids: list[str] = Field(min_length=1, max_length=500)


class DeletionIn(DeletionPreviewIn):
    version: str = Field(min_length=1)  # the `version` of the preview that was read
    typed: dict[str, str] = Field(default_factory=dict)  # unit id -> the identifier typed


class RestoreIn(BaseModel):
    deletion_id: str | None = None  # one deletion (the latest when absent)
    partial: bool = False  # the files that can go back do, the others stay in the bin


class ProbeIn(BaseModel):
    folder: str = Field(min_length=1)


def build_library_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["library"])

    @router.get("/adoption/{destination_id}/pairing", response_model=list[Body])
    def pair_propose(destination_id: str, _admin: Admin) -> list[Body]:
        with refusals():
            return get_ops().library.pair_propose(destination_id)

    @router.post("/adoption/{destination_id}/pair", response_model=Body)
    def pair(destination_id: str, body: PairIn, _admin: Admin) -> Body:
        with refusals():
            return get_ops().library.pair_confirm(
                destination_id, body.export_series_id, source_key=body.source_key,
                remote_id=body.remote_id, follow_from=body.follow_from,
                confirm_backfill=body.confirm_backfill,
            )  # fmt: skip

    @router.post("/adoption/{destination_id}/pair-auto", response_model=Body)
    def pair_auto(destination_id: str, body: PairAutoIn, _admin: Admin) -> Body:
        with refusals():
            return get_ops().library.pair_auto(
                destination_id, min_score=body.min_score, fingerprint=body.fingerprint
            )

    @router.get("/adoption/{destination_id}/migration", response_model=Body)
    def migration(destination_id: str, _admin: Admin) -> Body:
        with refusals():
            return get_ops().library.migration_report(destination_id)

    @router.post("/adoption/{destination_id}/repatriate", response_model=Body)
    def repatriate(destination_id: str, body: RepatriateIn, _admin: Admin) -> Body:
        with refusals():
            library = get_ops().library
            if body.fingerprint is None:
                return library.repatriate_plan(destination_id, body.source)
            return library.repatriate_apply(
                destination_id, fingerprint=body.fingerprint, source=body.source
            )

    @router.post("/detach", response_model=Body)
    def restore_upstream(body: DetachIn, _admin: Admin) -> Body:
        with refusals():
            return get_ops().library.give_back_upstream(
                shelf_id=body.shelf_id, edition_id=body.edition_id
            )

    @router.post("/units/{unit_id}/purge", response_model=Body)
    def purge_unit(
        unit_id: str, body: PurgeIn, principal: Annotated[Principal, Depends(_PURGE)]
    ) -> Body:
        with refusals():
            return get_ops().library.purge(
                kind="unit", anchor_id=unit_id, typed=body.typed, principal=principal,
            )  # fmt: skip

    @router.post("/works/{work_id}/purge", response_model=Body)
    def purge_work(
        work_id: str, body: PurgeIn, principal: Annotated[Principal, Depends(_PURGE)]
    ) -> Body:
        with refusals():
            return get_ops().library.purge(
                kind="work", anchor_id=work_id, typed=body.typed, principal=principal,
            )  # fmt: skip

    @router.post("/deletions/preview", response_model=Body)
    def deletion_preview(
        body: DeletionPreviewIn, _principal: Annotated[Principal, Depends(_DELETE)]
    ) -> Body:
        with refusals():
            return get_ops().library.delete_preview(level=body.level, unit_ids=body.unit_ids)

    @router.post("/deletions", response_model=Body)
    def deletion_apply(body: DeletionIn, principal: Annotated[Principal, Depends(_DELETE)]) -> Body:
        with refusals():
            return get_ops().library.delete_apply(
                level=body.level, unit_ids=body.unit_ids, if_match=body.version,
                typed=body.typed, principal=principal,
            )  # fmt: skip

    @router.post("/units/{unit_id}/restore", response_model=Body)
    def deletion_restore(
        unit_id: str,
        principal: Annotated[Principal, Depends(_DELETE)],
        body: RestoreIn | None = None,
    ) -> Body:
        asked = body or RestoreIn()
        with refusals():
            return get_ops().library.delete_restore(
                unit_id=unit_id, principal=principal,
                deletion_id=asked.deletion_id, partial=asked.partial,
            )  # fmt: skip

    @router.post("/probe", response_model=Body)
    def probe(body: ProbeIn, _admin: Admin) -> Body:
        with refusals():
            return get_ops().library.probe(body.folder)

    return router
