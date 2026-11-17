"""The two doors of a bulk rating of "À classer" (M4 L28, EF-BIB-023).

`POST /bulk/rate/preview` freezes a selection (the Works chosen, or a filter) into an order and
rates nothing; `POST /bulk/rate/{order_id}/apply` names the order and its token, nothing else, and
answers `202 Accepted` with the `job_id` of the one job that rates them (and the `Location` of the
job). The server checks the selection again under the write lock, in the job.

Both are the reserved action `rate`: a fresh admin session holding the right to classify, never a
key (whatever its scope, read-only or not), never a member or a child; the answer is never kept
for an idempotent replay (the route is reserved). An order is read for the profile that previewed
it: another profile's order, a forged id and an expired preview are one answer, and the identifier
asked for is not echoed. The titles come from the sources: data for the client, never markup.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.accepted import JobAccepted, accepted_job
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.library.public import bulk
from shelfhold.ops import OpsServices

__all__ = ["build_bulk_router"]

_RATE = reserved_action(RESERVED_ACTIONS["rate"])
_ID = Annotated[str, Field(min_length=26, max_length=26, pattern="^[0-9A-Za-z]{26}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # nothing outside the contract rides along


class BulkFilter(_Strict):
    """A filter of the list, frozen by the preview into the Works it matched then."""

    state: Literal["rating_pending", "follow_suspended"] | None = None
    text: str | None = Field(default=None, max_length=200)


class BulkRatePreviewIn(_Strict):
    age: str = Field(min_length=1, max_length=8, examples=["13+", "18+", "X"])
    work_ids: list[_ID] | None = Field(default=None, max_length=bulk.MAX_SELECTION)
    filter: BulkFilter | None = None

    @model_validator(mode="after")
    def _one_selection(self) -> "BulkRatePreviewIn":
        """The Works chosen (at least one) or a filter: never both, never neither."""
        if (self.work_ids is None) == (self.filter is None) or self.work_ids == []:
            raise ValueError("selection_required")
        return self


class BulkItem(BaseModel):
    work_id: str
    title: str
    state: str


class BulkRatePreviewOut(BaseModel):
    order_id: str
    age: str
    token: str  # what the apply gives back
    skipped: int  # identifiers that do not wait in the list: counted, never named
    expires_at: str
    items: list[BulkItem]


class BulkRateApplyIn(_Strict):
    confirm: str = Field(min_length=1, max_length=64)


def build_bulk_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["bulk"])

    @router.post("/bulk/rate/preview", response_model=BulkRatePreviewOut)
    def bulk_rate_preview(
        body: BulkRatePreviewIn, principal: Annotated[Principal, Depends(_RATE)]
    ) -> BulkRatePreviewOut:
        """Freeze the Works chosen (or a filter) into an order; nothing is rated or queued."""
        with refusals():
            done = get_ops().bulk.rate_preview(
                age=body.age,
                work_ids=list(body.work_ids) if body.work_ids is not None else None,
                filter=body.filter.model_dump() if body.filter is not None else None,
                principal=principal,
            )
        return BulkRatePreviewOut(**done)

    @router.post(
        "/bulk/rate/{order_id}/apply", status_code=202, responses={202: {"model": JobAccepted}}
    )
    def bulk_rate_apply(
        order_id: Annotated[str, Path(min_length=26, max_length=26)],
        body: BulkRateApplyIn,
        principal: Annotated[Principal, Depends(_RATE)],
    ) -> Any:
        """Queue the one job that rates the order's Works; the job checks the selection again."""
        with refusals():
            job_id = get_ops().bulk.rate_queue(
                order_id=order_id, confirm=body.confirm, principal=principal
            )
        return accepted_job(job_id)

    return router
