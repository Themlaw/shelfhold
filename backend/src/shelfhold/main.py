"""Composition root: wires infrastructure into the HTTP interface."""

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold import __version__
from shelfhold.catalog.public import ensure_search_tables, sync_search_index
from shelfhold.composition import Composition, Network
from shelfhold.config import Settings, load_settings
from shelfhold.events.bus import EventBus
from shelfhold.events.since_key import SinceKey
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.wiring import open_pipeline, roots_signature
from shelfhold.headless import HeadlessAdmin, wiring
from shelfhold.headless.applied import record_applied_public_url
from shelfhold.identity.adapters.preferences import SqlNotificationPrefsStore, SqlPreferencesStore
from shelfhold.identity.adapters.user_state_store import user_state_service
from shelfhold.identity.application.notification_prefs import NotificationPrefsService
from shelfhold.identity.application.preferences import PreferencesService
from shelfhold.identity.public import PasskeyRecord, local_auth, reconcile_identity_counter
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.health import HealthProbe
from shelfhold.infrastructure.idempotency_store import IdempotencyStore
from shelfhold.infrastructure.identity_counter import IdentityCounter
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
from shelfhold.interfaces.http_api.audit import build_audit_router
from shelfhold.interfaces.http_api.auth import AuthGate
from shelfhold.interfaces.http_api.backfill import build_backfill_router
from shelfhold.interfaces.http_api.catalog_admin import build_catalog_admin_router
from shelfhold.interfaces.http_api.catalog_lists import build_catalog_lists_router
from shelfhold.interfaces.http_api.destinations import build_destinations_router
from shelfhold.interfaces.http_api.follows import build_follows_router
from shelfhold.interfaces.http_api.headless import build_headless_router
from shelfhold.interfaces.http_api.health import build_health_page_router
from shelfhold.interfaces.http_api.identity import (
    build_identity_router,
    build_notification_prefs_router,
)
from shelfhold.interfaces.http_api.identity_headers import ForwardAuth
from shelfhold.interfaces.http_api.intents import build_intents_router
from shelfhold.interfaces.http_api.jobs import build_jobs_router
from shelfhold.interfaces.http_api.library import build_library_router
from shelfhold.interfaces.http_api.maintenance import build_maintenance_router
from shelfhold.interfaces.http_api.me import build_me_router
from shelfhold.interfaces.http_api.ops import build_ops_router
from shelfhold.interfaces.http_api.plans import build_plans_router
from shelfhold.interfaces.http_api.plugins import build_plugins_router
from shelfhold.interfaces.http_api.requests import build_requests_router
from shelfhold.interfaces.http_api.revisions import build_revisions_router
from shelfhold.interfaces.http_api.search import build_search_router
from shelfhold.interfaces.http_api.series_sheet import build_series_sheet_router
from shelfhold.interfaces.http_api.setup import build_setup_router
from shelfhold.interfaces.http_api.shelves import build_edition_shelf_router, build_shelves_router
from shelfhold.interfaces.http_api.sources import build_sources_router
from shelfhold.interfaces.http_api.stats import build_stats_router
from shelfhold.interfaces.http_api.status import build_status_router
from shelfhold.interfaces.http_api.stream import build_stream_router
from shelfhold.interfaces.http_api.thumbnails import build_thumbnails_router
from shelfhold.interfaces.http_api.triage import build_triage_router
from shelfhold.interfaces.local_cli import IdentityDispatcher
from shelfhold.jobs.mode import MaintenanceMode
from shelfhold.library.public import ThumbnailService
from shelfhold.ops import OpsServices
from shelfhold.ops.activity_feed import kavita_open_url_of
from shelfhold.ops.health import OpsHealth
from shelfhold.ops.notifications import subscribe_notifications
from shelfhold.ops.requests import build_service, resolve_request_ref, subscribe_arrivals
from shelfhold.ops.wiring import open_ops
from shelfhold.plugin_host.attestation import AttestationStore
from shelfhold.plugin_host.public import PluginRegistry, installed_versions, start_plugins
from shelfhold.plugin_host.settings import SettingsSchema, SourceSettings, parse_settings_schema
from shelfhold.runtime import RETAKE_INTERVAL_S, InstanceRuntime
from shelfhold.runtime_jobs import PlanService

PACKAGED_FRONTEND = Path(__file__).parent / "static"


class Readiness:
    """False until the start-up has run to its end: an instance that waits for a migration's lock
    serves no authenticated request (503) before it owns the volumes and has finished."""

    def __init__(self) -> None:
        self._ready = False

    def set(self, value: bool) -> None:
        self._ready = value

    def is_set(self) -> bool:
        return self._ready


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
        with write_transaction(engine) as connection:
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
    "import_dir": "import_dir",
    "host": "host", "port": "port", "log_level": "log_level", "log_format": "log_format",
    "umask": "umask", "public_url": "public_url", "frontend_dir": "frontend_dir",
    "plugin_repos": "plugin_repos", "active_sources": "active_sources",
    "source_url": "source_url", "received_cbz_max_bytes": "received_cbz_max_bytes",
    "naming_segment_policy_confirmed": "naming_segment_policy_confirmed",
    "wake_url": "wake_url", "wake_suwayomi": "wake_suwayomi", "wake_kavita": "wake_dest",
    "wake_timeout_s": "wake_timeout_s", "wake_session_s": "wake_duration_s",
    "fetcher_cache": "fetcher_cache", "fetcher_cache_off_sources": "fetcher_cache_off_sources",
    "cli_allow_host": "cli_allow_host", "allow_network_data_dir": "allow_network_data_dir",
    "disk_pause_percent": "disk_pause_percent", "disk_pause_gib": "disk_pause_gib",
    "disk_resume_percent": "disk_resume_percent", "disk_resume_gib": "disk_resume_gib",
    "auth_hardened": "hardened_profile",  # a switch
    "fresh_auth_s": "recent_login_window_s",  # a duration
    "revision_min_page_ratio": "revision_min_page_ratio",
    "follow_fallback_return": "follow_fallback_return",
    "follow_check_min_s": "follow_check_min_s",
    "follow_check_max_s": "follow_check_max_s",
    "follow_check_finished_s": "follow_check_finished_s",
    "secret_key_file": "trousseau_path",  # where the keyring is: a path, not the keyring
    "http_trusted_proxies": "proxy_trusted_cidrs",
    "auth_forward_enabled": "proxy_identity_enabled",  # a switch
    "http_public_rate_per_minute": "public_rate_per_minute",
    "http_device_rate_per_minute": "device_rate_per_minute",  # networks, not credentials
    "maintenance_window_start": "maintenance_window_start",
    "maintenance_window_end": "maintenance_window_end", "timezone": "timezone",
    "retention_events_days": "retention_events_days",
    "retention_audit_days": "retention_audit_days",
    "stream_max_per_profile": "stream_max_per_profile",
}  # fmt: skip
SECRET_SETTINGS = frozenset(
    {
        "api_token",
        "wake_token",
        "backup_passphrase_file",
        "metadata_contact",
        "auth_forward_secret",
    }
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
    subscribe_arrivals(composition, build_service(composition))  # EF-USR-010: a book arrived
    subscribe_notifications(composition)  # EF-INT-021: who is told of it, and when
    composition.pools = lambda: runtime.pools

    def passkey_registered(record: PasskeyRecord) -> None:
        # Owner decision 2026-11-12: any registration, any account, tells the admin (identifiers
        # only: the name of a passkey is typed by a person and never goes in an event)
        with write_transaction(engine) as connection:
            composition.bus.publish(
                connection,
                "auth.passkey_registered",
                {"user_id": record.user_id, "passkey_id": record.id},
            )

    auth = local_auth(engine, resolved, clock, on_passkey_registered=passkey_registered)
    ready = Readiness()
    gate = AuthGate(auth, public_url=resolved.public_url, ready=ready.is_set)

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

    plugins: list[PluginRegistry] = []  # the start-up load, once this process owns /data

    def schema_for(key: str) -> SettingsSchema | None:
        record = plugins[0].get(key) if plugins else None
        if record is None or record.manifest is None or record.status.value == "incompatible":
            return None
        return parse_settings_schema(record.manifest.settings_schema)

    def load_plugins() -> None:
        registry, _records = start_plugins(resolved)
        installed = installed_versions(resolved)
        if installed:  # EF-EXP-065: nothing to attest without a third-party plugin
            # the installed versions recorded, the attestations of another version revoked
            # (audited), then the admin's word given back to the plugins it was given for
            attestations = AttestationStore(engine, clock=clock)
            attestations.sync_installed(installed)
            registry.apply_attestations(attestations.valid())
        plugins[:] = [registry]
        composition.plugins = lambda: plugins[0] if plugins else None

    @cache
    def headless_admin() -> HeadlessAdmin:
        return wiring.open_headless(resolved)  # opened at first use, after the keyring check

    @cache
    def ops_services() -> OpsServices:
        def revoke(task_id: str) -> None:  # a queued job cancelled or re-ranked: its Huey task
            if runtime.pools is not None:
                runtime.pools.revoke(task_id)

        return open_ops(
            resolved, engine=engine, composition=composition, revoke=revoke,
            flagged_names=lambda destination_id: plans().flagged_names(destination_id),
        )  # fmt: skip

    @cache
    def plans() -> PlanService:
        return PlanService(composition, pipeline, lambda: runtime.pools)

    since_key = SinceKey(composition.keyring)  # `?since=` tokens and SSE cursors: one key

    export_routers = (
        build_shelves_router(lambda: ShelfService(engine, clock=clock), ops_services),
        build_destinations_router(ops_services),
        build_edition_shelf_router(lambda: ShelfService(engine, clock=clock)),
        build_headless_router(headless_admin, lambda: resolved.public_url),
        build_setup_router(ops_services),  # L39: the first-run wizard, screens 2 and 3
        build_plans_router(plans),
        build_jobs_router(ops_services),  # EF-INT-001: the URL of a 202
        build_audit_router(ops_services),  # EF-EXP-036: the Kavita accounts of the children
        build_maintenance_router(ops_services),  # EF-INT-018, EF-INT-017: mode and queue control
        build_intents_router(ops_services),  # EF-INT-014: the open intents and their settlement
        build_revisions_router(ops_services),  # EF-BIB-043: the queue of pending revisions
        build_backfill_router(ops_services),  # EF-EXP-053: estimate, confirmation, start
        # EF-BIB-008, EF-BIB-003, ENF-I18N-003, EF-BIB-044: locks, renumbering, sorted list, resplit
        build_catalog_admin_router(ops_services),
        build_follows_router(ops_services),  # EF-SRC-029: follow, unfollow
        build_series_sheet_router(ops_services),  # M3 L43: the series sheet and its settings
        build_requests_router(  # EF-USR-010: ask, approve, refuse, follow a request
            lambda: ops_services().requests,
            lambda token, owner: resolve_request_ref(ops_services().acquire, token, owner),
            lambda: kavita_open_url_of(engine),  # M3 L41: "Ouvrir dans Kavita"
        ),
        build_sources_router(ops_services),  # EF-SRC-015, EF-SRC-004: search, acquire, a URL
        build_me_router(cache(lambda: user_state_service(engine, clock))),  # EF-USR-007, EF-BIB-022
        build_activity_router(lambda: engine),  # EF-USR-016: `activity_view`, unmask audited
        build_catalog_lists_router(lambda: engine, since_key),  # M3 L11: `?since=`
        build_stream_router(
            lambda: engine,
            lambda: composition.bus,
            clock,
            since_key,
            max_per_profile=resolved.stream_max_per_profile,
        ),  # M3 L12 (EF-INT-002): typed SSE, one filter per reader
        build_thumbnails_router(  # M3 L24: a cover at 160/320/640, session only, authorised first
            lambda: engine, cache(lambda: ThumbnailService(engine, resolved.data_dir, clock))
        ),
        build_search_router(
            lambda: engine, ops_services
        ),  # EF-BIB-010, EF-SRC-005: admin only (L09)
        build_identity_router(
            lambda: PreferencesService(SqlPreferencesStore(engine))
        ),  # EF-UX-012: theme and colour scheme per user
        build_notification_prefs_router(
            lambda: NotificationPrefsService(SqlNotificationPrefsStore(engine))
        ),  # EF-INT-021: how a person wants to be told, their own row only
        build_stats_router(lambda: engine, clock.now),  # EF-INT-025: admin only, local only
        build_status_router(ops_services),  # EF-INT-023: what waits, with the command
        build_health_page_router(lambda: engine, resolved, clock.now),  # M3 L15: admin only
        build_ops_router(ops_services),  # EF-INT-023: the exits
        build_triage_router(ops_services),  # EF-USR-015, EF-EXP-046: the two lists, the batch
        build_library_router(ops_services),  # pairing, repatriation, detach, purge, probe
        # EF-SRC-013, EF-EXP-065: per-source settings (nothing secret read back), attestations
        build_plugins_router(
            lambda: SourceSettings(engine, composition.vault, now=clock.now),
            lambda: AttestationStore(engine, clock=clock),
            schema_for,
        ),
    )

    def on_shutdown() -> None:
        runtime.shutdown()  # SIGTERM: stop claiming, checkpoint, free the locks (ENF-OPS-007)
        runtime.stop()
        health.close()  # a read of the status still running would open a connection after
        engine.dispose()

    def start_owned() -> None:
        """What follows the lock, in the order of the spec; also run when the lock is taken
        later, once the migration that held it has ended."""
        # Before any request: a database older than the identity counter of /data (put back by
        # hand, or by `backup restore`) has its sessions and API keys revoked, with a notice.
        if runtime.owns_volumes:
            acted = reconcile_identity_counter(
                engine, IdentityCounter(resolved.data_dir), now=clock.now()
            )
            if acted is not None:
                structlog.get_logger().warning(
                    "instance.credentials_revoked_after_restore",
                    sessions=acted[0], api_keys=acted[1],
                )  # fmt: skip
        check_schema_not_newer(resolved)  # ENF-OPS-002: refuse to run on a newer schema
        try:
            boot_check(resolved)  # ENF-SEC-005: an unreadable or missing keyring stops it
        except KeyringError as error:
            announce_keyring_failure(engine, composition.bus, error)  # `secret.undecryptable`
            raise
        runtime.boot()  # open intents settled, then (and only then) the jobs
        if runtime.owns_volumes:  # a migration in progress: this process writes nothing
            record_applied_public_url(engine, resolved.public_url)  # `status` compares
            sync_search()

    def sync_search() -> None:
        """EF-BIB-010: the search index follows the catalog (a Work written by a path that does not
        index it, a SQLite that gained FTS5): never a reason not to start."""
        try:
            with write_transaction(engine) as connection:
                ensure_search_tables(connection)
                sync_search_index(connection)
        except sa.exc.SQLAlchemyError as error:
            structlog.get_logger().warning("search.sync_failed", error=type(error).__name__)

    health = OpsHealth(HealthProbe(resolved), ops_services)

    def on_startup() -> None:
        log_effective_config(resolved)
        ensure_volumes(resolved)
        runtime.acquire()  # ENF-OPS-015: first, before anything else; a second instance refuses
        try:
            check_schema_not_newer(resolved)  # ENF-OPS-002: refuse to run on a newer schema
            # EF-SRC-019: third-party code only once this process owns /data and the schema is ours
            load_plugins()
            start_owned()
            ready.set(runtime.owns_volumes)  # waiting for a migration's lock: not ready yet
            if runtime.owns_volumes:
                health.prime()  # the first `/healthz` finds the shared status already read
        except BaseException:
            runtime.stop()
            raise

    async def open_socket() -> LocalSocketServer | None:
        """The socket of the host CLI, then the projections (ENF-OPS-020). None: it is
        unavailable and the CLI works on the database."""
        if resolved.cli_allow_host:
            structlog.get_logger().warning("local_cli.allow_host_enabled")
        server = LocalSocketServer(
            resolved.data_dir / SOCKET_NAME,
            IdentityDispatcher(
                auth,
                headless=headless_admin(),
                ops=ops_services(),
                on_change=health.refresh,
            ),
            allow_host=resolved.cli_allow_host,
        )
        bound: LocalSocketServer | None = server
        try:
            await server.start()
        except OSError as error:  # the HTTP server stays up; the CLI then works on the database
            structlog.get_logger().error("local_cli.socket_unavailable", error=str(error))
            bound = None
        runtime.start_projection_repair()  # the socket is bound (or not): now the projections
        return bound

    async def take_over_when_free() -> LocalSocketServer | None:
        """The start found `/data/.lock` held by a migration and wrote nothing (it said so:
        `instance.waiting_for_lock`). Look again until it is free, then finish the start-up.
        Until then every authenticated request answers 503; if the late start fails, the process
        stops (it must not serve a half-started instance)."""
        try:
            while not await asyncio.to_thread(runtime.try_take_over):
                await asyncio.sleep(RETAKE_INTERVAL_S)
            await asyncio.to_thread(start_owned)
        except Exception:
            structlog.get_logger().exception("instance.start_after_wait_failed")
            runtime.stop()
            os.kill(os.getpid(), signal.SIGTERM)  # the server's own graceful stop
            return None
        ready.set(True)
        return await open_socket()

    @asynccontextmanager
    async def local_cli_socket() -> AsyncIterator[None]:
        """EF-USR-024: the host CLI reaches the running server here, and only here. Only the
        instance that owns the volumes listens; one that waits for a migration's lock listens
        once it has taken it."""
        waiting: asyncio.Future[LocalSocketServer | None] | None = None
        server = None
        if runtime.owns_volumes:
            server = await open_socket()  # bound before the first request
        else:
            waiting = asyncio.ensure_future(take_over_when_free())
        try:
            yield
        finally:
            if waiting is not None:
                if not waiting.done():
                    waiting.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    server = await waiting
            if server is not None:
                await server.stop()

    return create_http_app(
        health=health,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        background=local_cli_socket,
        version=__version__,
        frontend_dir=resolved.frontend_dir or PACKAGED_FRONTEND,
        routers=(*routers, *export_routers),
        auth=gate,
        idempotency=IdempotencyStore(engine, clock=clock.now),  # actor: the real principal
        source_url=resolved.source_url,
        trusted_proxies=resolved.http_trusted_proxies,
        forward_auth=(
            ForwardAuth(
                secret=resolved.auth_forward_secret.get_secret_value()
                if resolved.auth_forward_secret is not None
                else None
            )
            if resolved.auth_forward_enabled
            else None
        ),
        public_rate_per_minute=resolved.http_public_rate_per_minute,
        device_rate_per_minute=resolved.http_device_rate_per_minute,
        maintenance_active=MaintenanceMode(engine, clock).probe,  # EF-INT-018
    )


def app_from_env() -> HttpApplication:
    """Application factory used by uvicorn (`serve`)."""
    settings = load_settings(os.environ, db_loader=load_db_settings).values
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    return create_app(settings)
