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
# M3 L22: the guard writes no line (`audit=False`): the ops layer writes it, with the target and the
# session, in the transaction that acts; a plan that is only read or a refusal leaves none (#209)
_WORK_MERGE = reserved_action(RESERVED_ACTIONS["work.merge"], audit=False)
_WORK_SEPARATE = reserved_action(RESERVED_ACTIONS["work.separate"], audit=False)
_SERIES_REASSIGN = reserved_action(RESERVED_ACTIONS["series.reassign"], audit=False)
_EDITION_MERGE = reserved_action(RESERVED_ACTIONS["edition.merge"], audit=False)
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


class SuggestionOut(BaseModel):
    id: str
    a: str
    b: str
    reason: str
    state: str


class SuggestionsOut(BaseModel):
    items: list[SuggestionOut]


class ScanOut(BaseModel):
    created: list[str]  # the suggestions opened now; no Work was merged


class ExportItemOut(BaseModel):
    id: str
    edition_id: str
    kind: str
    label: str | None
    closed: bool
    units: int
    kept: str | None
    kept_problem: str | None


Id = Annotated[str, Field(min_length=1, max_length=64)]
Confirm = Annotated[str | None, Field(default=None, min_length=1, max_length=256)]


class MergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep_id: Id  # the one that stays
    drop_id: Id  # the one that goes into it
    confirm: Confirm = None  # the fingerprint the plan gave; none: only the plan is returned


class ContributorsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep_id: Id  # the contributor that stays
    drop_id: Id  # the one merged into it


class SeriesMoveOut(BaseModel):
    edition_id: str
    remote_library_id: str
    from_series_id: str
    to_series_id: str | None  # None with a `new_name`: a series of its own, started for the Work
    new_name: str | None = None


class SeriesReassignOut(BaseModel):
    work_id: str
    moves: list[SeriesMoveOut]
    blockers: list[str]
    path_changes: int  # moves out of a folder already written: the next path differs (EF-EXP-037)
    emptied: list[str]
    mergeable: bool
    fingerprint: str
    applied: bool


class WorkMergeOut(BaseModel):
    keep_id: str
    drop_id: str
    editions: list[str]
    blockers: list[str]
    keep_age: str | None
    drop_age: str | None
    merged_age: str | None
    unrated_after: bool
    mergeable: bool
    fingerprint: str
    applied: bool
    merge_id: str | None
    series_plan: "SeriesReassignOut | None" = None  # what the merge proposes for the exports


class SeparateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeparateOut(BaseModel):
    merge_id: str
    survivor_id: str
    restored_id: str
    shelves: dict[str, str | None]  # each Edition of the two Works: the shelf it is filed on
    waiting: dict[str, str]  # the ones the shelf no longer admits, never moved here
    survivor_age: str | None  # the age each half has now (`13+`, `X`, none: unrated)
    restored_age: str | None
    series_plan: SeriesReassignOut  # the plan for the Work given back, applied on confirmation


class SeriesReassignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_id: Id
    confirm: Confirm = None


class AttachSeriesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: Id
    position: str = Field(pattern=r"^[0-9]+(\.[0-9]+)?$", max_length=16)


class AttachSeriesOut(BaseModel):
    work_id: str
    series_id: str
    position: str
    reassignment: SeriesReassignOut  # the plan that follows; nothing in it was applied


class EditionMergeOut(BaseModel):
    keep_id: str
    drop_id: str
    work_id: str
    units: list[str]
    volumes: list[str]
    bindings: list[str]
    blockers: list[str]
    tombstoned_units: int
    tombstoned_ids: int
    keep_age: str | None
    drop_age: str | None
    merged_age: str | None
    unrated_after: bool
    mergeable: bool
    fingerprint: str
    applied: bool


class EditionMergeProposalsOut(BaseModel):
    items: list[EditionMergeOut]  # plans only: nothing is applied by a proposal


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

    @router.get("/merge-suggestions/works")
    def work_suggestions(_admin: Admin) -> SuggestionsOut:
        with refusals():
            rows = get_ops().dedup.suggestions()
        return SuggestionsOut(items=[SuggestionOut(**r) for r in rows])

    @router.post("/merge-suggestions/works/scan")
    def work_suggestions_scan(_admin: Admin) -> ScanOut:
        with refusals():
            return ScanOut(**get_ops().dedup.scan())

    @router.post("/works/merge")
    def works_merge(
        body: MergeIn, principal: Annotated[Principal, Depends(_WORK_MERGE)]
    ) -> WorkMergeOut:
        with refusals():
            done = get_ops().work_merge.merge(
                body.keep_id, body.drop_id, confirm=body.confirm, principal=principal
            )
        return WorkMergeOut(**done)

    @router.post("/works/merges/{merge_id}/separate")
    def works_separate(
        merge_id: str,
        principal: Annotated[Principal, Depends(_WORK_SEPARATE)],
        body: SeparateIn | None = None,
    ) -> SeparateOut:
        del body
        with refusals():
            return SeparateOut(**get_ops().work_merge.separate(merge_id, principal=principal))

    @router.post("/series-reassignments")
    def series_reassignments(
        body: SeriesReassignIn, principal: Annotated[Principal, Depends(_SERIES_REASSIGN)]
    ) -> SeriesReassignOut:
        with refusals():
            done = get_ops().work_merge.series_reassign(
                body.work_id, confirm=body.confirm, principal=principal
            )
        return SeriesReassignOut(**done)

    @router.post("/works/{work_id}/series")
    def works_attach_series(
        work_id: str, body: AttachSeriesIn, principal: Admin
    ) -> AttachSeriesOut:
        """A Series that comes after its Work (EF-BIB-025): the membership only."""
        with refusals():
            done = get_ops().work_merge.attach_series(
                work_id, body.series_id, body.position, principal=principal
            )
        return AttachSeriesOut(**done)

    @router.post("/editions/merge")
    def editions_merge(
        body: MergeIn, principal: Annotated[Principal, Depends(_EDITION_MERGE)]
    ) -> EditionMergeOut:
        with refusals():
            done = get_ops().edition_merge.merge(
                body.keep_id, body.drop_id, confirm=body.confirm, principal=principal
            )
        return EditionMergeOut(**done)

    @router.post("/contributors/merge-proposals")
    def contributors_merge_proposals(
        body: ContributorsIn, principal: Annotated[Principal, Depends(_EDITION_MERGE)]
    ) -> EditionMergeProposalsOut:
        """What merging `drop_id` into `keep_id` (two contributors) would call for: the merge
        plans of the Editions that would then share a key (EF-BIB-049). Nothing is applied."""
        with refusals():
            plans = get_ops().edition_merge.proposals(
                body.keep_id, body.drop_id, principal=principal
            )
        return EditionMergeProposalsOut(items=[EditionMergeOut(**p) for p in plans])

    @router.get("/export-items/{item_id}")
    def export_item(item_id: str, _admin: Admin) -> ExportItemOut:
        with refusals():
            return ExportItemOut(**get_ops().export_items.show(item_id))

    return router
