"""The maintenance mode and the control of the queue, over HTTP (EF-INT-018, EF-INT-017).

`MaintenanceGuard` is the ASGI gate: while the mode is on, a request that is not a read is refused
with 503 `maintenance.active` before it reaches a route, reserved routes included. Open: reading
(`GET`, `HEAD`, `OPTIONS`, so `/healthz` as well), the sign-in routes (a session has to be fresh to
leave the mode, and signing in writes only the session), and the exit itself.

The routes are the twins of `shelfhold maintenance on|off` and of the queue commands, and call the
same services. Entering and leaving are reserved (a fresh session, never a key); the queue commands
are reserved too, so a key, a device token, a member or a child never reaches them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import ANONYMOUS_JSON_PATHS, SAFE_METHODS, reserved_action
from shelfhold.interfaces.http_api.catalog import error_code
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import build_problem_response
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.jobs.mode import ACTIVE_CODE
from shelfhold.ops import OpsServices

__all__ = ["EXIT_PATH", "MaintenanceGuard", "build_maintenance_router"]

EXIT_PATH = "/api/v1/maintenance/exit"
OPEN_PATHS = frozenset({EXIT_PATH, *ANONYMOUS_JSON_PATHS})

_ENTER = reserved_action(RESERVED_ACTIONS["maintenance.enter"])
_EXIT = reserved_action(RESERVED_ACTIONS["maintenance.exit"])
_QUEUE = reserved_action(RESERVED_ACTIONS["queue.control"])
Admin = Annotated[Principal, Depends(require_admin)]


class MaintenanceGuard:
    def __init__(self, app: ASGIApp, *, active: Callable[[], bool]) -> None:
        self.app = app
        self._active = active

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] not in SAFE_METHODS
            and scope["path"] not in OPEN_PATHS
            and self._active()
        ):
            request_id = scope.get("state", {}).get("request_id")
            response = build_problem_response(
                error_code(ACTIVE_CODE),
                request_id=request_id if isinstance(request_id, str) else None,
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SpanOut(BaseModel):
    start_utc: str
    end_utc: str
    start_local: str
    end_local: str
    duration_s: int
    open: bool


class WindowOut(BaseModel):
    timezone: str
    start: str
    end: str
    crosses_midnight: bool
    current: SpanOut | None
    next: SpanOut
    upcoming: list[SpanOut]


class MaintenanceOut(BaseModel):
    active: bool
    since: str | None
    running_jobs: int  # still draining when the mode was entered


class QueueStateOut(BaseModel):
    paused: list[str]  # `all` and the sources
    parked_jobs: int


class MaintenanceStatusOut(BaseModel):
    maintenance: MaintenanceOut
    queue: QueueStateOut
    window: WindowOut


class ModeOut(BaseModel):
    active: bool
    changed: bool  # False: it was already so (nothing was written)
    running_jobs: int | None = None


class ScopeIn(BaseModel):
    source: str | None = Field(default=None, min_length=1, max_length=200)  # None: the whole queue


class PausedOut(BaseModel):
    scope: str
    parked: int


class ResumedOut(BaseModel):
    scope: str
    resumed: int


class CancelOut(BaseModel):
    id: str
    outcome: str  # cancelled | cancelling


class PriorityIn(BaseModel):
    priority_class: str = Field(min_length=1, max_length=20)


class PriorityOut(BaseModel):
    id: str
    priority_class: str


class ReplayOut(BaseModel):
    id: str  # the job that is queued after the call
    replayed: bool  # False: it was already queued, or another one holds its key


def build_maintenance_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["maintenance"])

    @router.get("/maintenance")
    def maintenance_status(_admin: Admin) -> MaintenanceStatusOut:
        with refusals():
            return MaintenanceStatusOut.model_validate(get_ops().maintenance.status())

    @router.post("/maintenance/enter")
    def maintenance_enter(principal: Annotated[Principal, Depends(_ENTER)]) -> ModeOut:
        with refusals():
            return ModeOut(**get_ops().maintenance.enter(principal=principal))

    @router.post("/maintenance/exit")
    def maintenance_exit(principal: Annotated[Principal, Depends(_EXIT)]) -> ModeOut:
        with refusals():
            return ModeOut(**get_ops().maintenance.leave(principal=principal))

    @router.post("/queue/pause")
    def queue_pause(body: ScopeIn, principal: Annotated[Principal, Depends(_QUEUE)]) -> PausedOut:
        with refusals():
            return PausedOut(**get_ops().maintenance.pause(body.source, principal=principal))

    @router.post("/queue/resume")
    def queue_resume(body: ScopeIn, principal: Annotated[Principal, Depends(_QUEUE)]) -> ResumedOut:
        with refusals():
            return ResumedOut(**get_ops().maintenance.resume(body.source, principal=principal))

    @router.post("/jobs/{job_id}/cancel")
    def job_cancel(job_id: str, principal: Annotated[Principal, Depends(_QUEUE)]) -> CancelOut:
        with refusals():
            return CancelOut(**get_ops().maintenance.cancel(job_id, principal=principal))

    @router.post("/jobs/{job_id}/priority")
    def job_priority(
        job_id: str, body: PriorityIn, principal: Annotated[Principal, Depends(_QUEUE)]
    ) -> PriorityOut:
        with refusals():
            done: dict[str, Any] = get_ops().maintenance.set_priority(
                job_id, body.priority_class, principal=principal
            )
            return PriorityOut(**done)

    @router.post("/jobs/{job_id}/replay")
    def job_replay(job_id: str, principal: Annotated[Principal, Depends(_QUEUE)]) -> ReplayOut:
        with refusals():
            return ReplayOut(**get_ops().maintenance.replay(job_id, principal=principal))

    return router
