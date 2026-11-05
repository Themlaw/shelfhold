"""Composition root: wires infrastructure into the HTTP interface."""

import os
from functools import cache
from pathlib import Path

from shelfhold import __version__
from shelfhold.config import Settings, load_settings
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.wiring import open_pipeline
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.health import HealthProbe
from shelfhold.infrastructure.idempotency_store import IdempotencyStore
from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.migrate import check_schema_not_newer
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.settings_store import load_db_settings
from shelfhold.infrastructure.volumes import ensure_volumes
from shelfhold.interfaces.http_api.app import HttpApplication, Routers, create_http_app
from shelfhold.interfaces.http_api.auth import AuthGate
from shelfhold.interfaces.http_api.plans import build_plans_router
from shelfhold.interfaces.http_api.shelves import build_shelves_router
from shelfhold.runtime import InstanceRuntime

PACKAGED_FRONTEND = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    routers: Routers = (),
) -> HttpApplication:
    resolved = settings or load_settings(os.environ, db_loader=load_db_settings).values
    clock = clock or SystemClock()

    runtime = InstanceRuntime(resolved)
    # Opened lazily: no connection before the first request, after the start-up checks.
    engine = create_db_engine(resolved.database_path)
    gate = AuthGate(local_auth(engine, resolved, clock), public_url=resolved.public_url)

    @cache
    def pipeline() -> object:
        # Plans can be previewed; applying one needs the Kavita `plugin/parse` check (L29).
        return open_pipeline(resolved)

    export_routers = (
        build_shelves_router(lambda: ShelfService(engine)),
        build_plans_router(pipeline),  # type: ignore[arg-type]
    )

    def on_shutdown() -> None:
        runtime.stop()
        engine.dispose()

    def on_startup() -> None:
        ensure_volumes(resolved)
        runtime.acquire()  # ENF-OPS-015: first, before anything else; a second instance refuses
        try:
            check_schema_not_newer(resolved)  # ENF-OPS-002: refuse to run on a newer schema
            boot_check(resolved)  # ENF-SEC-005: an unreadable or missing keyring stops the start
            runtime.boot()  # open intents settled, then (and only then) the jobs
        except BaseException:
            runtime.stop()
            raise

    return create_http_app(
        health=HealthProbe(resolved),
        on_startup=on_startup,
        on_shutdown=on_shutdown,
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
