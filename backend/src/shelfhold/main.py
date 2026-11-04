"""Composition root: wires infrastructure into the HTTP interface."""

import os
from pathlib import Path

from shelfhold import __version__
from shelfhold.config import Settings, load_settings
from shelfhold.infrastructure.health import HealthProbe
from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.migrate import check_schema_not_newer
from shelfhold.infrastructure.settings_store import load_db_settings
from shelfhold.infrastructure.volumes import ensure_volumes
from shelfhold.interfaces.http_api.app import HttpApplication, create_http_app

PACKAGED_FRONTEND = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> HttpApplication:
    resolved = settings or load_settings(os.environ, db_loader=load_db_settings).values

    def on_startup() -> None:
        ensure_volumes(resolved)
        check_schema_not_newer(resolved)  # ENF-OPS-002: refuse to run on a newer schema

    return create_http_app(
        health=HealthProbe(resolved),
        on_startup=on_startup,
        version=__version__,
        frontend_dir=resolved.frontend_dir or PACKAGED_FRONTEND,
    )


def app_from_env() -> HttpApplication:
    """Application factory used by uvicorn (`serve`)."""
    settings = load_settings(os.environ, db_loader=load_db_settings).values
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    return create_app(settings)
