"""`/healthz` (liveness, migration progress) and `/readyz` (readiness), ENF-OPS-003."""

from collections.abc import Mapping
from typing import Literal, Protocol

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
    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]


class HealthPort(Protocol):
    def migration_progress(self) -> tuple[int, int] | None:
        """`(done, total)` while a migration runs, else None."""
        ...

    def readiness(self) -> Mapping[str, bool]: ...


def build_health_router(health: HealthPort) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz", response_model=HealthzResponse, response_model_exclude_none=True)
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

    @router.get(
        "/readyz", response_model=ReadyzResponse, responses={503: {"model": ReadyzResponse}}
    )
    def readyz() -> JSONResponse:
        checks = dict(health.readiness())
        ready = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    return router
