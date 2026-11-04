"""HTTP application factory (FastAPI lives only in `interfaces/`)."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.routing import APIRoute
from starlette.requests import Request

from shelfhold.infrastructure.http_headers import ROBOTS_TXT, apply_security_headers
from shelfhold.interfaces.http_api.health import HealthPort, build_health_router
from shelfhold.interfaces.http_api.middleware import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from shelfhold.interfaces.http_api.spa import mount_spa

HttpApplication = FastAPI


def _operation_id(route: APIRoute) -> str:
    return route.name


def create_http_app(
    *,
    health: HealthPort,
    on_startup: Callable[[], None],
    version: str,
    frontend_dir: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        on_startup()
        yield

    app = FastAPI(
        title="Shelfhold",
        version=version,
        lifespan=lifespan,
        openapi_url=None,  # A6: no anonymous route; the snapshot is exported offline
        docs_url=None,
        redoc_url=None,
        generate_unique_id_function=_operation_id,  # stable names for the generated client
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)  # outermost: wraps every response

    @app.exception_handler(Exception)
    def unhandled(request: Request, _exc: Exception) -> Response:
        # Raised outside the middleware stack: the 500 needs the headers applied by hand.
        response = JSONResponse({"code": "internal_error"}, status_code=500)
        apply_security_headers(response.headers)
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.api_route("/robots.txt", methods=["GET", "HEAD"], include_in_schema=False)
    def robots() -> Response:
        return PlainTextResponse(ROBOTS_TXT)

    app.include_router(build_health_router(health))
    if frontend_dir is not None:
        mount_spa(app, frontend_dir)  # last: the API routes keep priority
    return app
