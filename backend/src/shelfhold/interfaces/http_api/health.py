"""`/healthz` (liveness, migration progress) and `/readyz` (readiness), ENF-OPS-003."""

from collections.abc import Mapping
from typing import Protocol

from fastapi import APIRouter
from fastapi.responses import JSONResponse


class HealthPort(Protocol):
    def migration_progress(self) -> tuple[int, int] | None:
        """`(done, total)` while a migration runs, else None."""
        ...

    def readiness(self) -> Mapping[str, bool]: ...


def build_health_router(health: HealthPort, version: str) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    def healthz() -> JSONResponse:
        progress = health.migration_progress()
        if progress is not None:
            body: dict[str, object] = {
                "state": "migrating",
                "progress": {"done": progress[0], "total": progress[1]},
            }
        else:
            body = {"state": "ok", "version": version}
        return JSONResponse(body)

    @router.get("/readyz")
    def readyz() -> JSONResponse:
        checks = dict(health.readiness())
        ready = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    return router
