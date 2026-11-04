"""HTTP application factory (FastAPI lives only in `interfaces/`)."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shelfhold.interfaces.http_api.health import HealthPort, build_health_router
from shelfhold.interfaces.http_api.middleware import RequestContextMiddleware

HttpApplication = FastAPI


def create_http_app(*, health: HealthPort, on_startup: Callable[[], None], version: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        on_startup()
        yield

    app = FastAPI(
        title="Shelfhold", version=version, lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(build_health_router(health, version))
    return app
