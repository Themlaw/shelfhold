"""HTTP application factory (FastAPI lives only in `interfaces/`)."""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import PlainTextResponse, Response
from fastapi.routing import APIRoute

from shelfhold.infrastructure.http_headers import ROBOTS_TXT, require_web_url
from shelfhold.infrastructure.idempotency_store import IdempotencyStore
from shelfhold.interfaces.http_api.about import build_about_router, build_source_url
from shelfhold.interfaces.http_api.auth import (
    ANONYMOUS_JSON_PATHS,
    AuthGate,
    AuthMiddleware,
    install_auth,
)
from shelfhold.interfaces.http_api.auth import actor_of as principal_actor
from shelfhold.interfaces.http_api.device_auth import build_device_router
from shelfhold.interfaces.http_api.device_tokens import (
    build_device_tokens_router,
    build_devices_router,
)
from shelfhold.interfaces.http_api.health import HealthPort, build_health_router
from shelfhold.interfaces.http_api.idempotency import (
    ActorOf,
    IdempotencyMiddleware,
    ReservedRoutes,
)
from shelfhold.interfaces.http_api.identity_headers import (
    ForwardAuth,
    IdentityHeaderFilter,
    parse_networks,
)
from shelfhold.interfaces.http_api.login import build_login_router
from shelfhold.interfaces.http_api.maintenance import MaintenanceGuard
from shelfhold.interfaces.http_api.middleware import (
    HealthRefreshMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from shelfhold.interfaces.http_api.passkeys import build_passkeys_router
from shelfhold.interfaces.http_api.problems import (
    default_problem_responses,
    install_problem_handlers,
    install_problem_openapi,
)
from shelfhold.interfaces.http_api.profiles import (
    build_own_visibility_router,
    build_profiles_router,
)
from shelfhold.interfaces.http_api.ratelimit import (
    ClientIpMiddleware,
    PublicRateLimit,
    RecognisedCredentials,
)
from shelfhold.interfaces.http_api.sessions import build_sessions_router
from shelfhold.interfaces.http_api.spa import mount_spa
from shelfhold.interfaces.http_api.webhooks import install_webhooks_openapi

HttpApplication = FastAPI
Routers = Sequence[APIRouter]  # extra routers, named so the composition root needs no FastAPI


def _operation_id(route: APIRoute) -> str:
    return route.name


def create_http_app(
    *,
    health: HealthPort,
    on_startup: Callable[[], None],
    on_shutdown: Callable[[], None] | None = None,
    background: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    version: str,
    frontend_dir: Path | None = None,
    routers: Sequence[APIRouter] = (),
    idempotency: IdempotencyStore | None = None,
    actor_of: ActorOf | None = None,
    auth: AuthGate | None = None,
    source_url: str | None = None,
    trusted_proxies: Sequence[str] = (),  # D15: whose X-Forwarded-For and identity are read
    forward_auth: ForwardAuth | None = None,  # EF-USR-002: None = off (the default)
    public_rate_per_minute: int | None = None,  # ENF-SEC-008; None: no limit (bare tests)
    device_rate_per_minute: int = 600,
    device_allow_plain_http: bool = False,  # M4 L46, ADR-0053 §4: TLS unless the admin says so
    maintenance_active: Callable[[], bool | None] | None = None,
    on_profile_change: Callable[[], None] | None = None,  # M4 L18b: a ceiling, a profile removed
) -> FastAPI:
    """Without `auth` (offline schema export, protocol tests) the authentication routes are still
    declared, so that the exported schema documents them, but nobody is ever authenticated."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        on_startup()
        try:
            # Services that live as long as the application (the local CLI socket), started
            # once the instance owns the volumes and stopped before it lets them go.
            async with background() if background is not None else nullcontext():
                yield
        finally:
            if on_shutdown is not None:
                on_shutdown()

    app = FastAPI(
        title="Shelfhold",
        version=version,
        lifespan=lifespan,
        redirect_slashes=False,  # ENF-SEC-016: a redirect would build its Location from `Host`
        openapi_url=None,  # A6: no anonymous route; the snapshot is exported offline
        docs_url=None,
        redoc_url=None,
        generate_unique_id_function=_operation_id,  # stable names for the generated client
        responses=default_problem_responses(),  # RFC 9457 (EF-INT-001)
    )
    reserved = ReservedRoutes()  # the routes whose keyed POST is never kept (M4 L34)
    install_problem_handlers(app)
    install_problem_openapi(app, public_paths=ANONYMOUS_JSON_PATHS)
    install_webhooks_openapi(app)  # M4 L24: `x-webhooks`, what a receiver is sent
    install_auth(app, auth)
    app.state.health = health
    refresh = getattr(health, "refresh", None)
    if callable(refresh):  # innermost: only a request that went through to a route
        app.add_middleware(HealthRefreshMiddleware, refresh=refresh)
    if auth is not None:
        actor_of = actor_of or principal_actor
    if idempotency is not None:
        if actor_of is None:  # a ledger without actors would share keys between users
            raise ValueError("idempotency_requires_actor_of")
        app.add_middleware(
            IdempotencyMiddleware, store=idempotency, actor_of=actor_of,
            reserved=reserved,
        )  # fmt: skip
    if auth is not None and public_rate_per_minute is not None:
        app.state.recognised_devices = RecognisedCredentials()
        app.add_middleware(  # inside auth
            PublicRateLimit,
            per_minute=public_rate_per_minute,
            per_credential=device_rate_per_minute,
            recognised=app.state.recognised_devices,
        )
    if auth is not None:
        app.add_middleware(AuthMiddleware, gate=auth)  # outside the ledger: the real actor
    if maintenance_active is not None:  # EF-INT-018: inside the request context, outside auth
        app.add_middleware(MaintenanceGuard, active=maintenance_active)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)  # wraps every response
    networks = parse_networks(trusted_proxies)
    app.state.device_allow_plain_http = device_allow_plain_http
    app.add_middleware(ClientIpMiddleware, trusted=networks)  # the real address, for the limits
    app.add_middleware(  # outermost: nothing below ever sees a proxy identity header (P5)
        IdentityHeaderFilter,
        trusted=networks,
        forward=forward_auth,
        on_first_strip=auth.record_stripped_header if auth is not None else None,
        on_rejected_route=auth.record_forward_rejection if auth is not None else None,
    )

    @app.api_route("/robots.txt", methods=["GET", "HEAD"], include_in_schema=False)
    def robots() -> Response:
        return PlainTextResponse(ROBOTS_TXT)

    def include(router: APIRouter) -> None:
        reserved.add(router)
        app.include_router(router)

    include(build_health_router(health, version=version))
    resolved_source = require_web_url(source_url or build_source_url(version))
    include(build_login_router(resolved_source))
    include(build_passkeys_router())
    include(build_sessions_router())
    include(build_profiles_router(on_profile_change))
    include(build_own_visibility_router())  # EF-USR-016
    include(build_about_router(version=version, source_url=resolved_source))
    include(build_device_tokens_router())  # M4 L46b: pairing an e-reader
    include(build_devices_router())  # M5 L02: the registry of devices, rotation, removal
    include(build_device_router())  # M4 L46: the e-reader's own credential
    for router in routers:
        include(router)
    if frontend_dir is not None:
        mount_spa(app, frontend_dir)  # last: the API routes keep priority
    return app
