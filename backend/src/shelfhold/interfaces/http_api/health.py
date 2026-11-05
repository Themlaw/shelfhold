"""`/healthz` (liveness, migration progress) and `/readyz` (readiness), ENF-OPS-003.

Anonymous callers get the state only (O12, A7). A session or an API key also gets the version and
the paused queues on `/healthz`, and the check names on `/readyz` (L05).
"""

from collections.abc import Mapping
from typing import Literal, Protocol

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


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
        body: dict[str, object] = {"state": "ready" if ready else "not_ready"}
        if _authenticated(request):
            body["checks"] = checks
        return JSONResponse(body, status_code=200 if ready else 503)

    return router
