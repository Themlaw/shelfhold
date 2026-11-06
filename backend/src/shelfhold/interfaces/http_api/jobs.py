"""`GET /api/v1/jobs/{id}`: the URL that a `202 Accepted` gives for a long operation (EF-INT-001).

The client polls it until `status` leaves `queued` and `running`. A job that failed carries the
catalog `error_code` of the refusal (`plan.stale`, `target.route_missing`...), never a message: an
upstream text may name a title (EF-BIB-053).
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices

__all__ = ["build_jobs_router"]

Admin = Annotated[Principal, Depends(require_admin)]


class JobStatusOut(BaseModel):
    id: str
    kind: str
    status: str  # queued | running | paused | succeeded | failed | dead_letter | cancelled
    error_code: str | None
    attempts: int
    progress: float | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


def build_jobs_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["jobs"])

    @router.get("/jobs/{job_id}")
    def job_status(job_id: str, _admin: Admin) -> JobStatusOut:
        with refusals():
            row = get_ops().job_get(job_id)
        return JobStatusOut(
            id=row.id, kind=row.kind, status=row.status, error_code=row.error_code,
            attempts=row.attempts, progress=row.progress, created_at=row.created_at,
            started_at=row.started_at, finished_at=row.finished_at,
        )  # fmt: skip

    return router
