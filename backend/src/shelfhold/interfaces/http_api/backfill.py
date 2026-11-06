"""`/api/v1/editions/{id}/backfill`: fill the holes or bring the whole history back (EF-EXP-053).

`GET` is the estimate (Units, size, free space) and starts nothing. `POST` starts the backfill;
above 100 Units or 5 GiB it needs `confirm_units` and `confirm_bytes`, the figures the caller saw,
and otherwise answers 409 `backfill.confirmation_required` with the estimate taken now. Not a
reserved action: it asks for downloads within the per-cycle caps, it deletes nothing.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices

__all__ = ["build_backfill_router"]

Admin = Annotated[Principal, Depends(require_admin)]
Mode = Literal["gaps", "all"]


class EstimateOut(BaseModel):
    mode: Mode
    units: int
    size_bytes: int
    free_bytes: int
    needs_confirmation: bool


class BackfillIn(BaseModel):
    mode: Mode
    confirm_units: int | None = Field(default=None, ge=0)
    confirm_bytes: int | None = Field(default=None, ge=0)


def build_backfill_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["backfill"])

    @router.get("/editions/{edition_id}/backfill")
    def backfill_estimate(edition_id: str, _admin: Admin, mode: Mode = "gaps") -> EstimateOut:
        with refusals():
            return EstimateOut(**get_ops().backfill.estimate(edition_id, mode))

    @router.post("/editions/{edition_id}/backfill")
    def backfill_start(edition_id: str, body: BackfillIn, _admin: Admin) -> EstimateOut:
        with refusals():
            answer = get_ops().backfill.start(
                edition_id, body.mode, confirm_units=body.confirm_units,
                confirm_bytes=body.confirm_bytes,
            )  # fmt: skip
        started = answer.pop("started")
        estimate = EstimateOut(**answer)
        if not started:
            raise ProblemError(
                "backfill.confirmation_required", extensions={"estimate": estimate.model_dump()}
            )
        return estimate

    return router
