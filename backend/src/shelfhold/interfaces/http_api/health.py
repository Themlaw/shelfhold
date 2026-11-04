"""`/healthz` (liveness, migration progress) and `/readyz` (readiness), ENF-OPS-003."""

from collections.abc import Mapping
from typing import Literal, Protocol

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class MigrationProgress(BaseModel):
    done: int
    total: int


class HealthzResponse(BaseModel):
    state: Literal["ok", "migrating"]  # anonymous: no names, no version (O12, A7)
    progress: MigrationProgress | None = None


class ReadyzResponse(BaseModel):
    state: Literal["ready", "not_ready"]  # check names are for authenticated callers (L05)


class HealthPort(Protocol):
    def migration_progress(self) -> tuple[int, int] | None:
        """`(done, total)` while a migration runs, else None."""
        ...

    def readiness(self) -> Mapping[str, bool]: ...


def build_health_router(health: HealthPort) -> APIRouter:
    router = APIRouter()

    @router.head("/healthz", include_in_schema=False)
    @router.get(
        "/healthz",
        response_model=HealthzResponse,
        response_model_exclude_none=True,
    )
    def healthz() -> JSONResponse:
        progress = health.migration_progress()
        if progress is not None:
            body: dict[str, object] = {
                "state": "migrating",
                "progress": {"done": progress[0], "total": progress[1]},
            }
        else:
            body = {"state": "ok"}
        return JSONResponse(body)

    @router.head("/readyz", include_in_schema=False)
    @router.get(
        "/readyz",
        response_model=ReadyzResponse,
        responses={503: {"model": ReadyzResponse}},
    )
    def readyz() -> JSONResponse:
        checks = dict(health.readiness())
        failed = sorted(name for name, passed in checks.items() if not passed)
        if failed:
            structlog.get_logger().warning("readiness_failed", checks=failed)
        ready = not failed
        return JSONResponse(
            {"state": "ready" if ready else "not_ready"},
            status_code=200 if ready else 503,
        )

    return router
