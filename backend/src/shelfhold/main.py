"""Composition root: wires infrastructure into the HTTP interface."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path

import structlog

from shelfhold import __version__
from shelfhold.composition import Composition, Network
from shelfhold.config import Settings, load_settings
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.wiring import open_pipeline
from shelfhold.headless import HeadlessAdmin, wiring
from shelfhold.headless.applied import record_applied_public_url
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.health import HealthProbe
from shelfhold.infrastructure.idempotency_store import IdempotencyStore
from shelfhold.infrastructure.local_socket import SOCKET_NAME, LocalSocketServer
from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.migrate import check_schema_not_newer
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.settings_store import load_db_settings
from shelfhold.infrastructure.volumes import ensure_volumes
from shelfhold.interfaces.http_api.app import HttpApplication, Routers, create_http_app
from shelfhold.interfaces.http_api.auth import AuthGate
from shelfhold.interfaces.http_api.headless import build_headless_router
from shelfhold.interfaces.http_api.library import build_library_router
from shelfhold.interfaces.http_api.ops import build_ops_router
from shelfhold.interfaces.http_api.plans import build_plans_router
from shelfhold.interfaces.http_api.shelves import build_shelves_router
from shelfhold.interfaces.http_api.stats import build_stats_router
from shelfhold.interfaces.http_api.status import build_status_router
from shelfhold.interfaces.local_cli import IdentityDispatcher
from shelfhold.ops import OpsServices
from shelfhold.ops.health import OpsHealth
from shelfhold.ops.wiring import open_ops
from shelfhold.runtime import InstanceRuntime

PACKAGED_FRONTEND = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    routers: Routers = (),
    network: Network | None = None,
) -> HttpApplication:
    resolved = settings or load_settings(os.environ, db_loader=load_db_settings).values
    clock = clock or SystemClock()

    runtime = InstanceRuntime(resolved)
    # Opened lazily: no connection before the first request, after the start-up checks.
    engine = create_db_engine(resolved.database_path)
    # One composition per process: the bus with its channels, the lease, the tombstones. The
    # runtime's jobs and the routes below share it; `network` is the one door to other servers.
    composition = Composition(resolved, engine, clock=clock, network=network, gates=runtime.gates())
    runtime.composition = composition
    auth = local_auth(engine, resolved, clock)
    gate = AuthGate(auth, public_url=resolved.public_url)

    @cache
    def pipeline() -> object:
        # Plans can be previewed; applying one needs the Kavita `plugin/parse` check (L29).
        return open_pipeline(
            resolved,
            write_gate=runtime.require_space,
            stop_gate=runtime.barrier.require_open,
            gate=runtime.require_writable,
        )

    @cache
    def headless_admin() -> HeadlessAdmin:
        return wiring.open_headless(resolved)  # opened at first use, after the keyring check

    @cache
    def ops_services() -> OpsServices:
        return open_ops(resolved, engine=engine, composition=composition)  # lazy parts

    export_routers = (
        build_shelves_router(lambda: ShelfService(engine)),
        build_headless_router(headless_admin, lambda: resolved.public_url),
        build_plans_router(pipeline),  # type: ignore[arg-type]
        build_stats_router(lambda: engine, clock.now),  # EF-INT-025: admin only, local only
        build_status_router(ops_services),  # EF-INT-023: what waits, with the command
        build_ops_router(ops_services),  # EF-INT-023: the exits
        build_library_router(ops_services),  # pairing, repatriation, detach, purge, probe
    )

    def on_shutdown() -> None:
        runtime.shutdown()  # SIGTERM: stop claiming, checkpoint, free the locks (ENF-OPS-007)
        runtime.stop()
        engine.dispose()

    def on_startup() -> None:
        ensure_volumes(resolved)
        runtime.acquire()  # ENF-OPS-015: first, before anything else; a second instance refuses
        try:
            check_schema_not_newer(resolved)  # ENF-OPS-002: refuse to run on a newer schema
            boot_check(resolved)  # ENF-SEC-005: an unreadable or missing keyring stops the start
            runtime.boot()  # open intents settled, then (and only then) the jobs
            if runtime.owns_volumes:  # a migration in progress: this process writes nothing
                record_applied_public_url(engine, resolved.public_url)  # `status` compares
        except BaseException:
            runtime.stop()
            raise

    @asynccontextmanager
    async def local_cli_socket() -> AsyncIterator[None]:
        """EF-USR-024: the host CLI reaches the running server here, and only here. Only the
        instance that owns the volumes listens (a second one never got this far)."""
        if not runtime.owns_volumes:
            yield
            return
        if resolved.cli_allow_host:
            structlog.get_logger().warning("local_cli.allow_host_enabled")
        server = LocalSocketServer(
            resolved.data_dir / SOCKET_NAME,
            IdentityDispatcher(auth, headless=headless_admin(), ops=ops_services()),
            allow_host=resolved.cli_allow_host,
        )
        try:
            await server.start()
        except OSError as error:  # the HTTP server stays up; the CLI then works on the database
            structlog.get_logger().error("local_cli.socket_unavailable", error=str(error))
            yield
            return
        try:
            yield
        finally:
            await server.stop()

    return create_http_app(
        health=OpsHealth(HealthProbe(resolved), ops_services),
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        background=local_cli_socket,
        version=__version__,
        frontend_dir=resolved.frontend_dir or PACKAGED_FRONTEND,
        routers=(*routers, *export_routers),
        auth=gate,
        idempotency=IdempotencyStore(engine, clock=clock.now),  # actor: the real principal
        source_url=resolved.source_url,
        forward_auth_trusted_cidrs=resolved.forward_auth_trusted_cidrs,
    )


def app_from_env() -> HttpApplication:
    """Application factory used by uvicorn (`serve`)."""
    settings = load_settings(os.environ, db_loader=load_db_settings).values
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    return create_app(settings)
