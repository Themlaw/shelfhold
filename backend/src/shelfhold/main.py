"""Composition root: wires infrastructure into the HTTP interface."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold import __version__
from shelfhold.composition import Composition, Network
from shelfhold.config import Settings, load_settings
from shelfhold.events.bus import EventBus
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.wiring import open_pipeline, roots_signature
from shelfhold.headless import HeadlessAdmin, wiring
from shelfhold.headless.applied import record_applied_public_url
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.health import HealthProbe
from shelfhold.infrastructure.idempotency_store import IdempotencyStore
from shelfhold.infrastructure.local_socket import SOCKET_NAME, LocalSocketServer
from shelfhold.infrastructure.logging import configure_logging, strip_url_queries
from shelfhold.infrastructure.migrate import check_schema_not_newer
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.errors import (
    KeyringCorruptError,
    KeyringError,
    KeyringMissingError,
    KeyringPermissionsError,
)
from shelfhold.infrastructure.settings_store import load_db_settings
from shelfhold.infrastructure.volumes import ensure_volumes
from shelfhold.interfaces.http_api.activity import build_activity_router
from shelfhold.interfaces.http_api.app import HttpApplication, Routers, create_http_app
from shelfhold.interfaces.http_api.auth import AuthGate
from shelfhold.interfaces.http_api.backfill import build_backfill_router
from shelfhold.interfaces.http_api.follows import build_follows_router
from shelfhold.interfaces.http_api.headless import build_headless_router
from shelfhold.interfaces.http_api.intents import build_intents_router
from shelfhold.interfaces.http_api.jobs import build_jobs_router
from shelfhold.interfaces.http_api.library import build_library_router
from shelfhold.interfaces.http_api.ops import build_ops_router
from shelfhold.interfaces.http_api.plans import build_plans_router
from shelfhold.interfaces.http_api.shelves import build_edition_shelf_router, build_shelves_router
from shelfhold.interfaces.http_api.stats import build_stats_router
from shelfhold.interfaces.http_api.status import build_status_router
from shelfhold.interfaces.http_api.triage import build_triage_router
from shelfhold.interfaces.local_cli import IdentityDispatcher
from shelfhold.ops import OpsServices
from shelfhold.ops.health import OpsHealth
from shelfhold.ops.wiring import open_ops
from shelfhold.plugin_host.public import start_plugins
from shelfhold.runtime import InstanceRuntime
from shelfhold.runtime_jobs import PlanService

PACKAGED_FRONTEND = Path(__file__).parent / "static"
_KEYRING_REASONS: tuple[tuple[type[KeyringError], str], ...] = (
    (KeyringCorruptError, "keyring_corrupt"),
    (KeyringPermissionsError, "keyring_permissions"),
    (KeyringMissingError, "keyring_missing"),
)  # anything else of the keyring is "unreadable"


def announce_keyring_failure(engine: Engine, bus: EventBus, error: KeyringError) -> None:
    """The start stops on a keyring that cannot be read (ENF-SEC-005); the event goes on the
    operating bus first, if the database can take it (`secret.undecryptable`, ENF-OPS-011)."""
    reason = next((why for kind, why in _KEYRING_REASONS if isinstance(error, kind)), None)
    try:
        with engine.begin() as connection:
            bus.publish(
                connection, "secret.undecryptable", {"reason": reason or "keyring_unreadable"}
            )
    except sa.exc.SQLAlchemyError:  # no database yet (first start): the log is all there is
        structlog.get_logger().error("secret.undecryptable", reason=reason)


# The effective configuration at start-up is a white list: a new setting is not logged until
# someone decides it holds no secret (a test makes the decision explicit). The right-hand name is
# the one in the log: the filter masks any key whose name looks like a secret (`auth`, `key`,
# `secret`), and these four are not secrets, so they are logged under names that do not match.
LOGGED_SETTINGS: dict[str, str] = {
    "backup_dir": "backup_dir", "backup_retention": "backup_retention",
    "backup_stale_hours": "backup_stale_hours",
    "data_dir": "data_dir", "store_dir": "store_dir", "library_dir": "library_dir",
    "host": "host", "port": "port", "log_level": "log_level", "log_format": "log_format",
    "umask": "umask", "public_url": "public_url", "frontend_dir": "frontend_dir",
    "plugin_repos": "plugin_repos", "active_sources": "active_sources",
    "source_url": "source_url", "received_cbz_max_bytes": "received_cbz_max_bytes",
    "naming_segment_policy_confirmed": "naming_segment_policy_confirmed",
    "wake_url": "wake_url", "wake_suwayomi": "wake_suwayomi", "wake_kavita": "wake_dest",
    "wake_timeout_s": "wake_timeout_s", "wake_session_s": "wake_duration_s",
    "cli_allow_host": "cli_allow_host", "allow_network_data_dir": "allow_network_data_dir",
    "disk_pause_percent": "disk_pause_percent", "disk_pause_gib": "disk_pause_gib",
    "disk_resume_percent": "disk_resume_percent", "disk_resume_gib": "disk_resume_gib",
    "auth_hardened": "hardened_profile",  # a switch
    "fresh_auth_s": "recent_login_window_s",  # a duration
    "secret_key_file": "trousseau_path",  # where the keyring is: a path, not the keyring
    "forward_auth_trusted_cidrs": "proxy_trusted_cidrs",  # networks, not credentials
    "maintenance_window_start": "maintenance_window_start",
    "maintenance_window_end": "maintenance_window_end", "timezone": "timezone",
    "retention_events_days": "retention_events_days",
    "retention_audit_days": "retention_audit_days",
}  # fmt: skip
SECRET_SETTINGS = frozenset(
    {"api_token", "wake_token", "backup_passphrase_file"}
)  # never logged, whatever its value


def log_effective_config(settings: Settings) -> None:
    """The effective configuration at start-up (INFO): the white list above, query strings cut
    off every URL, then the log filter on top."""
    dump = settings.model_dump(mode="json")
    config = {shown: strip_url_queries(dump[field]) for field, shown in LOGGED_SETTINGS.items()}
    structlog.get_logger().info("startup.config", version=__version__, config=config)


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

    opened: list[tuple[object, ExportPipeline]] = []

    def pipeline() -> ExportPipeline:
        # Plans are previewed here; the job `export.apply` reads the names from Kavita again.
        # Opened again when the roots changed since: a root added by `target add` while the
        # server runs must be known to the next preview.
        signature = roots_signature(engine)
        if not opened or opened[0][0] != signature:
            opened[:] = [
                (
                    signature,
                    open_pipeline(
                        resolved,
                        write_gate=runtime.require_space,
                        stop_gate=runtime.barrier.require_open,
                        gate=runtime.require_writable,
                    ),
                )
            ]
        return opened[0][1]

    @cache
    def headless_admin() -> HeadlessAdmin:
        return wiring.open_headless(resolved)  # opened at first use, after the keyring check

    @cache
    def ops_services() -> OpsServices:
        return open_ops(resolved, engine=engine, composition=composition)  # lazy parts

    @cache
    def plans() -> PlanService:
        return PlanService(composition, pipeline, lambda: runtime.pools)

    export_routers = (
        build_shelves_router(lambda: ShelfService(engine, clock=clock), ops_services),
        build_edition_shelf_router(lambda: ShelfService(engine, clock=clock)),
        build_headless_router(headless_admin, lambda: resolved.public_url),
        build_plans_router(plans),
        build_jobs_router(ops_services),  # EF-INT-001: the URL of a 202
        build_intents_router(ops_services),  # EF-INT-014: the open intents and their settlement
        build_backfill_router(ops_services),  # EF-EXP-053: estimate, confirmation, start
        build_follows_router(ops_services),  # EF-SRC-029: follow, unfollow
        build_activity_router(lambda: engine),  # EF-USR-016: `activity_view`, unmask audited
        build_stats_router(lambda: engine, clock.now),  # EF-INT-025: admin only, local only
        build_status_router(ops_services),  # EF-INT-023: what waits, with the command
        build_ops_router(ops_services),  # EF-INT-023: the exits
        build_triage_router(ops_services),  # EF-USR-015, EF-EXP-046: the two lists, the batch
        build_library_router(ops_services),  # pairing, repatriation, detach, purge, probe
    )

    def on_shutdown() -> None:
        runtime.shutdown()  # SIGTERM: stop claiming, checkpoint, free the locks (ENF-OPS-007)
        runtime.stop()
        engine.dispose()

    def on_startup() -> None:
        log_effective_config(resolved)
        ensure_volumes(resolved)
        runtime.acquire()  # ENF-OPS-015: first, before anything else; a second instance refuses
        try:
            check_schema_not_newer(resolved)  # ENF-OPS-002: refuse to run on a newer schema
            # EF-SRC-019: third-party code only once this process owns /data and the schema is ours
            start_plugins(resolved)
            try:
                boot_check(resolved)  # ENF-SEC-005: an unreadable or missing keyring stops it
            except KeyringError as error:
                announce_keyring_failure(engine, composition.bus, error)  # `secret.undecryptable`
                raise
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
            runtime.start_projection_repair()
            yield
            return
        runtime.start_projection_repair()  # the socket is bound: now the projections (ENF-OPS-020)
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
