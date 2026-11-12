"""`/healthz` (liveness, migration progress) and `/readyz` (readiness), ENF-OPS-003.

Anonymous callers get the state only (O12, A7). A session or an API key also gets the version and
the paused queues on `/healthz`, and the check names on `/readyz` (L05).
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops.health import build_health_page


class MigrationProgress(BaseModel):
    done: int
    total: int


class PausedQueue(BaseModel):
    queue: str
    reason: str  # the waiting state, or the error code of a job paused by the window


class HealthzResponse(BaseModel):
    state: Literal["ok", "migrating"]  # anonymous: no names, no version (O12, A7)
    progress: MigrationProgress | None = None
    version: str | None = None  # authenticated callers only
    paused_queues: list[str] | None = None  # authenticated callers only
    paused: list[PausedQueue] | None = None  # administrators only: the queues with their reason
    paused_reasons: dict[str, str] | None = None  # why: job kind -> code (ENF-OPS-011, D11)


class ReadyzResponse(BaseModel):
    state: Literal["ready", "not_ready"]
    checks: dict[str, bool] | None = None  # authenticated callers only


class HealthPort(Protocol):
    def migration_progress(self) -> tuple[int, int] | None:
        """`(done, total)` while a migration runs, else None."""
        ...

    def readiness(self) -> Mapping[str, bool]: ...


def _authenticated(request: Request) -> bool:
    return request.scope.get("state", {}).get("principal") is not None


def _paused_queues(health: HealthPort) -> list[str]:
    probe = getattr(health, "paused_queues", None)
    return sorted(probe()) if callable(probe) else []


def _paused_reasons(health: HealthPort) -> dict[str, str]:
    probe = getattr(health, "paused_reasons", None)
    return dict(sorted(probe().items())) if callable(probe) else {}


def _paused(health: HealthPort) -> list[dict[str, str]]:
    probe = getattr(health, "paused", None)
    return list(probe()) if callable(probe) else []


def _is_admin(request: Request) -> bool:
    principal = request.scope.get("state", {}).get("principal")
    return getattr(principal, "role", None) == "admin"


def build_health_router(health: HealthPort, *, version: str | None = None) -> APIRouter:
    router = APIRouter()

    @router.head("/healthz", include_in_schema=False)
    @router.get(
        "/healthz",
        response_model=HealthzResponse,
        response_model_exclude_none=True,
    )
    def healthz(request: Request) -> JSONResponse:
        progress = health.migration_progress()
        if progress is not None:
            body: dict[str, object] = {
                "state": "migrating",
                "progress": {"done": progress[0], "total": progress[1]},
            }
        else:
            body = {"state": "ok"}
        structlog.get_logger().debug("liveness.checked", migrating=progress is not None)
        if _authenticated(request):
            body["version"] = version
            body["paused_queues"] = _paused_queues(health) if progress is None else []
            body["paused_reasons"] = _paused_reasons(health) if progress is None else {}
            if _is_admin(request):  # the reason of each queue is for administrators (D11)
                body["paused"] = _paused(health) if progress is None else []
        return JSONResponse(body)

    @router.head("/readyz", include_in_schema=False)
    @router.get(
        "/readyz",
        response_model=ReadyzResponse,
        responses={503: {"model": ReadyzResponse}},
    )
    def readyz(request: Request) -> JSONResponse:
        checks = dict(health.readiness())
        failed = sorted(name for name, passed in checks.items() if not passed)
        if failed:
            structlog.get_logger().warning("readiness_failed", checks=failed)
        ready = not failed
        structlog.get_logger().debug("readiness.checked", ready=ready, checks=len(checks))
        body: dict[str, object] = {"state": "ready" if ready else "not_ready"}
        if _authenticated(request):
            body["checks"] = checks
        return JSONResponse(body, status_code=200 if ready else 503)

    return router


class ActionOut(BaseModel):
    command: str  # a command of the CLI that exists
    reserved: bool
    text: str


class SourceHealthOut(BaseModel):
    plugin_id: str
    state: Literal["ok", "paused", "degraded"]
    code: str | None  # stable; None when the source is well
    since: str | None
    action: ActionOut | None


class HealthPageOut(BaseModel):
    """The Health page (EF-SRC-014, EF-BIB-031, EF-BIB-051, EF-INT-015, EF-INT-025). The nested
    documents are typed by the code of their entries; none holds a secret, a phrase or a path."""

    generated_at: str
    sources: list[SourceHealthOut]
    irreplaceable: dict[str, Any]
    lost_originals: list[dict[str, Any]]
    withdrawal_pending: dict[str, Any]
    kavita_audit: list[dict[str, Any]]
    jurisdiction: dict[str, Any] | None
    backup: dict[str, Any]
    stats: dict[str, Any]


def build_health_page_router(
    get_engine: Callable[[], Engine], settings: Settings, now: Callable[[], datetime]
) -> APIRouter:
    """`GET /api/v1/health/page`: for the administrator alone (a member, a child or a key of
    either is refused before anything is read). Read only, from the database."""
    router = APIRouter(prefix="/api/v1", tags=["health"])

    @router.get("/health/page", response_model=HealthPageOut)
    def health_page(_admin: Annotated[Principal, Depends(require_admin)]) -> HealthPageOut:
        return HealthPageOut.model_validate(build_health_page(get_engine(), settings, now()))

    return router
