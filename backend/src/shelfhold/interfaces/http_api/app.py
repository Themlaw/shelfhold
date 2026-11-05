"""HTTP application factory (FastAPI lives only in `interfaces/`)."""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import PlainTextResponse, Response
from fastapi.routing import APIRoute

from shelfhold.infrastructure.http_headers import ROBOTS_TXT
from shelfhold.infrastructure.idempotency_store import IdempotencyStore
from shelfhold.interfaces.http_api.about import build_about_router, default_source_url
from shelfhold.interfaces.http_api.auth import (
    LOGIN_PATH,
    AuthGate,
    AuthMiddleware,
    install_auth,
)
from shelfhold.interfaces.http_api.auth import actor_of as principal_actor
from shelfhold.interfaces.http_api.health import HealthPort, build_health_router
from shelfhold.interfaces.http_api.idempotency import (
    ActorOf,
    IdempotencyMiddleware,
)
from shelfhold.interfaces.http_api.login import build_login_router
from shelfhold.interfaces.http_api.middleware import (
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from shelfhold.interfaces.http_api.problems import (
    default_problem_responses,
    install_problem_handlers,
    install_problem_openapi,
)
from shelfhold.interfaces.http_api.spa import mount_spa

HttpApplication = FastAPI
Routers = Sequence[APIRouter]  # extra routers, named so the composition root needs no FastAPI


def _operation_id(route: APIRoute) -> str:
    return route.name


def create_http_app(
    *,
    health: HealthPort,
    on_startup: Callable[[], None],
    on_shutdown: Callable[[], None] | None = None,
    version: str,
    frontend_dir: Path | None = None,
    routers: Sequence[APIRouter] = (),
    idempotency: IdempotencyStore | None = None,
    actor_of: ActorOf | None = None,
    auth: AuthGate | None = None,
    source_url: str | None = None,
) -> FastAPI:
    """Without `auth` (offline schema export, protocol tests) the authentication routes are still
    declared, so that the exported schema documents them, but nobody is ever authenticated."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        on_startup()
        try:
            yield
        finally:
            if on_shutdown is not None:
                on_shutdown()

    app = FastAPI(
        title="Shelfhold",
        version=version,
        lifespan=lifespan,
        openapi_url=None,  # A6: no anonymous route; the snapshot is exported offline
        docs_url=None,
        redoc_url=None,
        generate_unique_id_function=_operation_id,  # stable names for the generated client
        responses=default_problem_responses(),  # RFC 9457 (EF-INT-001)
    )
    install_problem_handlers(app)
    install_problem_openapi(app, public_paths=frozenset({LOGIN_PATH}))
    install_auth(app, auth)
    if auth is not None:
        actor_of = actor_of or principal_actor
    if idempotency is not None:
        if actor_of is None:  # a ledger without actors would share keys between users
            raise ValueError("idempotency_requires_actor_of")
        app.add_middleware(IdempotencyMiddleware, store=idempotency, actor_of=actor_of)
    if auth is not None:
        app.add_middleware(AuthMiddleware, gate=auth)  # outside the ledger: the real actor
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)  # outermost: wraps every response

    @app.api_route("/robots.txt", methods=["GET", "HEAD"], include_in_schema=False)
    def robots() -> Response:
        return PlainTextResponse(ROBOTS_TXT)

    app.include_router(build_health_router(health, version=version))
    resolved_source = source_url or default_source_url(version)
    app.include_router(build_login_router(resolved_source))
    app.include_router(build_about_router(version=version, source_url=resolved_source))
    for router in routers:
        app.include_router(router)
    if frontend_dir is not None:
        mount_spa(app, frontend_dir)  # last: the API routes keep priority
    return app
