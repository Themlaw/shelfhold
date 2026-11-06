"""`GET /api/v1/status`: what waits for a human, with the reason and the command (EF-INT-023).

The same list as `shelfhold status`; admin only, because it names the series, the files and the
accounts that wait. `/healthz` shows the paused queues to any authenticated caller (A7), and no
more.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices

__all__ = ["build_status_router"]


class WaitingOut(BaseModel):
    state: str
    queue: str
    subject: str
    reason: str
    command: str
    reserved: bool


class QueueOut(BaseModel):
    queue: str
    reason: str
    command: str


class StatusOut(BaseModel):
    waiting: list[WaitingOut]
    queues: list[QueueOut]


def build_status_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["operations"])

    @router.get("/status", response_model=StatusOut)
    def status(_admin: Annotated[Principal, Depends(require_admin)]) -> StatusOut:
        report = get_ops().status(full=False)
        return StatusOut(
            waiting=[WaitingOut(**vars(item)) for item in report.waiting],
            queues=[QueueOut(**vars(queue)) for queue in report.queues],
        )

    return router
