"""Composition root: wires infrastructure into the HTTP interface."""

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import structlog
from shelfhold_sdk.models import API_VERSION
from sqlalchemy import Engine

from shelfhold import __version__
from shelfhold.acquisition.public import RootGuard
from shelfhold.catalog.public import ensure_search_tables, sync_search_index
from shelfhold.composition import Composition, LazyTombstones, Network
from shelfhold.config import Settings, load_settings
from shelfhold.events.bus import EventBus
from shelfhold.events.since_key import SinceKey
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.wiring import open_pipeline, roots_signature
from shelfhold.export.targets.kavita import page_probe
from shelfhold.export.targets.komga import KomgaDestination
from shelfhold.headless import HeadlessAdmin, wiring
from shelfhold.headless.applied import record_applied_public_url
from shelfhold.identity.adapters.preferences import SqlNotificationPrefsStore, SqlPreferencesStore
from shelfhold.identity.adapters.user_state_store import user_state_service
from shelfhold.identity.application.notification_prefs import NotificationPrefsService
from shelfhold.identity.application.preferences import PreferencesService
from shelfhold.identity.public import PasskeyRecord, local_auth, reconcile_identity_counter
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.fsops import Fsops
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
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.settings_store import load_db_settings
from shelfhold.infrastructure.volumes import ensure_volumes
from shelfhold.integrations.notify import ProfileDestinations
from shelfhold.integrations.trackers import TrackerHost
from shelfhold.integrations.webhooks import WebhookAdmin
from shelfhold.interfaces.http_api.activity import build_activity_router
from shelfhold.interfaces.http_api.app import HttpApplication, Routers, create_http_app
from shelfhold.interfaces.http_api.audit import build_audit_router, build_komga_audit_router
from shelfhold.interfaces.http_api.auth import AuthGate
from shelfhold.interfaces.http_api.backfill import build_backfill_router
from shelfhold.interfaces.http_api.bootstrap import build_bootstrap_router
from shelfhold.interfaces.http_api.bulk import build_bulk_router
from shelfhold.interfaces.http_api.bundle_slices import build_bundle_slices_router
from shelfhold.interfaces.http_api.catalog_admin import build_catalog_admin_router
from shelfhold.interfaces.http_api.catalog_lists import build_catalog_lists_router
from shelfhold.interfaces.http_api.collections import build_collections_router
from shelfhold.interfaces.http_api.content_routes import build_content_router
from shelfhold.interfaces.http_api.destination_accounts import (
    build_destination_accounts_router,
)
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
from shelfhold.interfaces.http_api.kobo import build_kobo_router
from shelfhold.interfaces.http_api.kosync import build_kosync_router
from shelfhold.interfaces.http_api.library import build_library_router
from shelfhold.interfaces.http_api.lite import build_lite_router
from shelfhold.interfaces.http_api.maintenance import build_maintenance_router
from shelfhold.interfaces.http_api.me import build_me_router
from shelfhold.interfaces.http_api.metrics import build_metrics_router
from shelfhold.interfaces.http_api.notify_destination import build_notify_destination_router
from shelfhold.interfaces.http_api.opds import SeriesBundleProvider, build_opds_router
from shelfhold.interfaces.http_api.ops import build_ops_router
from shelfhold.interfaces.http_api.ops_dashboard import build_ops_dashboard_router
from shelfhold.interfaces.http_api.plans import build_plans_router
from shelfhold.interfaces.http_api.plugins import build_plugins_router
from shelfhold.interfaces.http_api.reader import build_reader_router
from shelfhold.interfaces.http_api.relations import build_relations_router
from shelfhold.interfaces.http_api.request_lists import build_request_lists_router
from shelfhold.interfaces.http_api.requests import build_requests_router
from shelfhold.interfaces.http_api.revisions import build_revisions_router
from shelfhold.interfaces.http_api.search import build_search_router
from shelfhold.interfaces.http_api.selections import build_selections_router
from shelfhold.interfaces.http_api.series_sheet import build_series_sheet_router
from shelfhold.interfaces.http_api.setup import build_setup_router
from shelfhold.interfaces.http_api.shelves import (
    build_edition_shelf_router,
    build_shelves_router,
    build_targets_router,
)
from shelfhold.interfaces.http_api.source_migration import build_source_migration_router
from shelfhold.interfaces.http_api.sources import build_sources_router
from shelfhold.interfaces.http_api.stats import build_stats_router
from shelfhold.interfaces.http_api.status import build_status_router
from shelfhold.interfaces.http_api.stream import build_stream_router
from shelfhold.interfaces.http_api.support_author import build_support_author_router
from shelfhold.interfaces.http_api.sync_status import build_sync_status_router
from shelfhold.interfaces.http_api.tachibk_import import build_tachibk_import_router
from shelfhold.interfaces.http_api.teams import build_teams_router
from shelfhold.interfaces.http_api.thumbnails import build_thumbnails_router
from shelfhold.interfaces.http_api.trackers import build_trackers_router
from shelfhold.interfaces.http_api.triage import build_triage_router
from shelfhold.interfaces.http_api.webhooks import build_webhooks_router
from shelfhold.interfaces.local_cli import IdentityDispatcher
from shelfhold.jobs.mode import MaintenanceMode
from shelfhold.library.public import ReadableCopies, RenditionService, ThumbnailService
from shelfhold.ops import OpsServices
from shelfhold.ops.activity_feed import kavita_open_url_of
from shelfhold.ops.health import OpsHealth
from shelfhold.ops.notifications import subscribe_notifications
from shelfhold.ops.requests import build_service, resolve_request_ref, subscribe_arrivals
from shelfhold.ops.wiring import open_ops
from shelfhold.plugin_host import lifecycle
from shelfhold.plugin_host.adapters.rpc.runner_socket import (
    connect_runner,
)
from shelfhold.plugin_host.attestation import AttestationStore
from shelfhold.plugin_host.index import RepoStore, index_fetcher
from shelfhold.plugin_host.install import MODE_ISOLATED, Layout, read_state, verdicts
from shelfhold.plugin_host.public import PluginRegistry, installed_versions, start_plugins
from shelfhold.plugin_host.registry import (
    InstalledPlugin,
    PluginOrigin,
    publish_plugin_event,
    third_party_entries,
)
from shelfhold.plugin_host.rpc.host_side import RpcSession
from shelfhold.plugin_host.rpc.protocol import RpcLimits
from shelfhold.plugin_host.settings import SettingsSchema, SourceSettings, parse_settings_schema
from shelfhold.progress_lease import build_progress_lease
from shelfhold.reading.application.content_access import (
    ContentAccess,
    ContentKey,
    NoReadableCopyError,
)
from shelfhold.reading.application.continue_reading import ContinueReading
from shelfhold.reading.application.kepub_delivery import KepubDelivery
from shelfhold.reading.application.lease import ProgressLease
from shelfhold.reading.application.page_stream import PageStream
from shelfhold.reading.application.positions import ReaderPositions
from shelfhold.reading.application.progress import ReadMarks
from shelfhold.reading.application.publication import PublicationService, epub_spine_of
from shelfhold.reading.application.settings import ReaderSettings
from shelfhold.reading.bundle import SeriesBundles
from shelfhold.reading.public import KoboSync, Kosync
from shelfhold.runtime import RETAKE_INTERVAL_S, InstanceRuntime
from shelfhold.runtime_jobs import RELATIONS_JOB_KIND, PlanService, hub_listeners

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


def series_bundles(
    engine: Engine, keyring: Callable[[], Keyring], now: Callable[[], datetime]
) -> SeriesBundleProvider | None:
    """The block of a series for the OPDS feed (ADR-0053 §2, §3, §8): one provider for the whole
    instance, since its limits (blocks at a time, per token) are counted in it."""
    return SeriesBundles(engine, keyring, now=now)


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
    "umask": "umask", "public_url": "public_url", "content_origin": "content_origin",
    "frontend_dir": "frontend_dir",
    "plugin_repos": "plugin_repos", "active_sources": "active_sources",
    "source_url": "source_url", "update_check_url": "update_check_url",
    "plugin_index_url": "plugin_index_url", "received_cbz_max_bytes": "received_cbz_max_bytes",
    "naming_segment_policy_confirmed": "naming_segment_policy_confirmed",
    "wake_url": "wake_url", "wake_suwayomi": "wake_suwayomi", "wake_kavita": "wake_dest",
    "wake_timeout_s": "wake_timeout_s", "wake_session_s": "wake_duration_s",
    "oidc_issuer": "oidc_issuer", "oidc_client_id": "oidc_client_id",
    "fetcher_cache": "fetcher_cache", "fetcher_cache_off_sources": "fetcher_cache_off_sources",
    "cli_allow_host": "cli_allow_host", "allow_network_data_dir": "allow_network_data_dir",
    "disk_pause_percent": "disk_pause_percent", "disk_pause_gib": "disk_pause_gib",
    "disk_resume_percent": "disk_resume_percent", "disk_resume_gib": "disk_resume_gib",
    "auth_hardened": "hardened_profile",  # a switch
    "metrics_enabled": "metrics_enabled",  # a switch
    "fresh_auth_s": "recent_login_window_s",  # a duration
    "oidc_max_age_s": "oidc_max_age_s",  # a duration
    "revision_min_page_ratio": "revision_min_page_ratio",
    "reading_relay_max_session_hours": "reading_relay_max_hours",  # a duration
    "follow_fallback_return": "follow_fallback_return",
    "page_target_format": "page_target_format",
    "page_target_quality": "page_target_quality",
    "follow_check_min_s": "follow_check_min_s",
    "follow_check_max_s": "follow_check_max_s",
    "follow_check_finished_s": "follow_check_finished_s",
    "secret_key_file": "trousseau_path",  # where the keyring is: a path, not the keyring
    "http_trusted_proxies": "proxy_trusted_cidrs",
    "auth_forward_enabled": "proxy_identity_enabled",  # a switch
    "http_public_rate_per_minute": "public_rate_per_minute",
    "http_device_rate_per_minute": "device_rate_per_minute",  # networks, not credentials
    "device_allow_plain_http": "device_allow_plain_http",  # a switch
    "maintenance_window_start": "maintenance_window_start",
    "maintenance_window_end": "maintenance_window_end", "timezone": "timezone",
    "retention_events_days": "retention_events_days",
    "retention_audit_days": "retention_audit_days",
    "stream_max_per_profile": "stream_max_per_profile",
    "adopt_probe_workers": "adopt_probe_workers",
    "comicvine_enabled": "comicvine_enabled",  # a switch
    "googlebooks_daily_budget": "googlebooks_daily_budget",  # a number, no secret
    "cache_thumbnails_quota_mib": "cache_thumbnails_quota_mib",
    "cache_thumbnails_retention_days": "cache_thumbnails_retention_days",
    "cache_renditions_quota_mib": "cache_renditions_quota_mib",
    "cache_renditions_retention_days": "cache_renditions_retention_days",
    "cache_http_quota_mib": "cache_http_quota_mib",
    "cache_http_retention_days": "cache_http_retention_days",
    "cache_metadata_rows": "cache_metadata_rows",
    "cache_metadata_quota_mib": "cache_metadata_quota_mib",
    "cache_plugin_quota_mib": "cache_plugin_quota_mib",  # quotas and ages, no secret
}  # fmt: skip
SECRET_SETTINGS = frozenset(
    {
        "api_token",
        "wake_token",
        "backup_passphrase_file",
        "metadata_contact",
        "comicvine_api_key",
        "oidc_client_secret",
        "auth_forward_secret",
        "setup_token",
    }
)  # never logged, whatever its value


def log_effective_config(settings: Settings) -> None:
    """The effective configuration at start-up (INFO): the white list above, query strings cut
    off every URL, then the log filter on top."""
    dump = settings.model_dump(mode="json")
    config = {shown: strip_url_queries(dump[field]) for field, shown in LOGGED_SETTINGS.items()}
    structlog.get_logger().info("startup.config", version=__version__, config=config)
    if settings.comicvine_enabled:  # the terms, said once whenever the source is switched on
        from shelfhold.plugin_host.adapters.native.comicvine import NOTICE, TERMS_URL

        structlog.get_logger().info("startup.comicvine", notice=NOTICE, terms_url=TERMS_URL)


ISOLATED_START_GRACE_S = 1.5  # start-up waits this long, at most, for the isolated plugins


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    routers: Routers = (),
    network: Network | None = None,
) -> HttpApplication:
    resolved = settings or load_settings(os.environ, db_loader=load_db_settings).values
    clock = clock or SystemClock()
    page_probe.configure(resolved.adopt_probe_workers)  # M4 L36: one bound for every preview

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

    auth = local_auth(
        engine,
        resolved,
        clock,
        on_passkey_registered=passkey_registered,
        network=composition.network,
    )
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

    isolated_sessions: list[RpcSession] = []
    isolated_start: list[asyncio.Task[None]] = []  # the background start, kept by reference

    async def stop_isolated() -> None:
        """Cancel the start still going, then close every isolated session it opened."""
        for task in isolated_start:
            task.cancel()
        for task in isolated_start:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        isolated_start.clear()
        while isolated_sessions:
            with contextlib.suppress(Exception):
                await isolated_sessions.pop().close()

    async def load_plugins() -> Callable[[], Coroutine[Any, Any, None]] | None:
        registry, _records = start_plugins(resolved)
        installed = installed_versions(resolved)
        if installed:  # EF-EXP-065: nothing to attest without a third-party plugin
            # the installed versions recorded, the attestations of another version revoked
            # (audited), then the admin's word given back to the plugins it was given for
            attestations = AttestationStore(engine, clock=clock)
            attestations.sync_installed(installed)
            registry.apply_attestations(attestations.valid())
        # M6 L09b (EF-SRC-030): a host upgrade that left a plugin behind is told, once
        judged = lifecycle.incompatible_installed(
            engine, host_version=lifecycle.HOST_VERSION, api_version=API_VERSION
        )
        if judged:
            lifecycle.announce_incompatible(engine, composition.bus, judged)
        plugins[:] = [registry]
        composition.plugins = lambda: plugins[0] if plugins else None

        # The core starts the granted isolated plugins: connect_runner, then load_isolated, and only
        # for the verdicts that are isolated and granted (never a disabled or in-process plugin).
        layout = Layout.of(resolved.data_dir)
        granted_isolated = [
            verdict.name
            for verdict in verdicts(
                layout, os.environ, revoked=lifecycle.revoked_dists(engine, layout)
            )
            if verdict.granted and verdict.mode == MODE_ISOLATED
        ]
        if not granted_isolated:
            return None
        # resolve each granted key to its entry point, reading the recorded distributions only
        state = read_state(layout)
        installed_plugins = [
            InstalledPlugin(i.name, i.top_levels) for i in state.installed.values()
        ]
        entries: list[tuple[str, str]] = third_party_entries(
            layout.site, installed_plugins, set(granted_isolated)
        )

        socket_path = os.environ.get("SHELFHOLD_PLUGIN_RUNNER_SOCKET", "/plugin-rpc/runner.sock")
        limits = RpcLimits()
        engine_local = engine

        def _sink(key: str, event_name: str, payload: Any) -> None:
            with write_transaction(engine_local) as connection:
                publish_plugin_event(composition.bus, connection, key, event_name, payload)

        async def start_isolated() -> None:
            for key, entry in entries:

                async def _connect(key: str = key, entry: str = entry) -> Any:
                    channel = await connect_runner(socket_path, key=key, entry=entry, limits=limits)
                    try:
                        session = await RpcSession.open(
                            channel, key=key, limits=limits, on_event=_sink
                        )
                    except BaseException:
                        await channel.close()
                        raise
                    isolated_sessions.append(session)
                    return session

                try:
                    await registry.load_isolated(key, PluginOrigin.THIRD_PARTY, _connect)
                except Exception as exc:  # a plugin that cannot start is reported, never fatal
                    structlog.get_logger().warning(
                        "plugin.isolated_start_failed", key=key, error=type(exc).__name__
                    )

        return start_isolated

    @cache
    def content_access() -> ContentAccess:  # M5 L05a: one, it holds the lists of the excerpts
        def fsops() -> Fsops:
            loaded = composition.load_roots()
            if loaded is None:
                raise NoReadableCopyError  # no library root: nothing to read
            return composition.open_fsops(loaded)

        return ContentAccess(
            engine, ContentKey(composition.keyring).value, clock.now,
            readable_copies(engine, fsops), content_origin=resolved.content_origin,
            hardened=resolved.auth_hardened,
        )  # fmt: skip

    @cache
    def page_stream() -> PageStream:  # M5 L18: the pages of an exported volume, by device token
        return PageStream(engine, content_access())

    @cache
    def renditions() -> RenditionService:  # M5 L18: an image for a client that lacks its format
        return RenditionService(
            engine, resolved.data_dir, clock, tombstones=LazyTombstones(composition)
        )

    @cache
    def kepubs() -> KepubDelivery:  # M5 L20b: a KEPUB for a Kobo, in OPDS and in `/lite`
        return KepubDelivery(engine, content_access(), renditions())

    @cache
    def thumbnails() -> ThumbnailService:  # the session route and the OPDS one share it
        return ThumbnailService(engine, resolved.data_dir, clock)

    @cache
    def webhook_admin() -> WebhookAdmin:
        return WebhookAdmin(
            engine, composition.vault, lambda: composition.network.resolver, clock.now
        )

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

    @cache
    def bundles() -> SeriesBundleProvider | None:  # M4 L49a: one, its counters are the instance's
        return series_bundles(engine, composition.keyring, clock.now)

    @cache
    def progress_lease() -> ProgressLease:  # M5 L22a: the lock of the Work a job is rewriting
        return build_progress_lease(engine, clock)

    @cache
    def kosync() -> Kosync:  # one: it counts the unknown fingerprints of each token
        return Kosync(
            engine, composition.keyring, clock.now,
            spine_of=epub_spine_of(content_access), hold_work=progress_lease(),
        )  # fmt: skip

    @cache
    def kobo_sync() -> KoboSync:  # M6 L17a: the sync of each device, on its own sync point
        return KoboSync(engine, clock.now)

    def relations_after_audience_change() -> None:
        pools = runtime.pools
        if pools is not None:  # M4 L19: the links a new audience forbids are taken back now
            pools.submit(RELATIONS_JOB_KIND, dedupe_key=RELATIONS_JOB_KIND)

    export_routers = (
        build_shelves_router(
            lambda: ShelfService(
                engine,
                clock=clock,
                root_check=RootGuard().check_root,
                on_audience_change=relations_after_audience_change,
                layout_check=KomgaDestination().layout_conflicts,  # L22: ADR-0052 at the binding
            ),
            ops_services,
        ),
        build_targets_router(lambda: ShelfService(engine, clock=clock)),  # M4 L33a: the servers
        build_destinations_router(ops_services),
        build_destination_accounts_router(ops_services),  # M4 L12: a member's own Kavita key
        build_notify_destination_router(  # M4 L39: where an adult's titles go, sealed
            lambda: ProfileDestinations(engine, composition.vault, clock)
        ),
        build_trackers_router(  # M6 L13: a profile's own tracker, sealed and hinted
            lambda: TrackerHost(
                engine,
                composition.vault,
                clock,
                registry_of=composition.plugins,
                fetcher_for=lambda origin: composition.network.one_shot(origin),
            )
        ),
        build_sync_status_router(ops_services),  # M4 L33b: the sync state, member by member
        build_edition_shelf_router(lambda: ShelfService(engine, clock=clock)),
        build_headless_router(headless_admin, lambda: resolved.public_url),
        build_bootstrap_router(resolved.setup_token),  # the first account, behind a secret
        build_setup_router(ops_services, headless_admin),  # L39: the first-run wizard
        build_plans_router(plans),
        build_jobs_router(ops_services),  # EF-INT-001: the URL of a 202
        build_audit_router(ops_services),  # EF-EXP-036: the Kavita accounts of the children
        build_komga_audit_router(ops_services),  # L22: the Komga accounts, on demand
        build_maintenance_router(ops_services),  # EF-INT-018, EF-INT-017: mode and queue control
        build_intents_router(ops_services),  # EF-INT-014: the open intents and their settlement
        build_revisions_router(ops_services),  # EF-BIB-043: the queue of pending revisions
        build_backfill_router(ops_services),  # EF-EXP-053: estimate, confirmation, start
        # EF-BIB-008, EF-BIB-003, ENF-I18N-003, EF-BIB-044: locks, renumbering, sorted list, resplit
        build_catalog_admin_router(ops_services),
        build_follows_router(ops_services),  # EF-SRC-029: follow, unfollow
        build_series_sheet_router(ops_services),  # M3 L43: the series sheet and its settings
        build_bundle_slices_router(lambda: engine, clock.now),  # M4 L49b: slices of a block
        build_support_author_router(lambda: engine),  # M4 L23: official links, pause proposal
        build_requests_router(  # EF-USR-010: ask, approve, refuse, follow a request
            lambda: ops_services().requests,
            lambda token, owner: resolve_request_ref(ops_services().acquire, token, owner),
            lambda: kavita_open_url_of(engine),  # M3 L41: "Ouvrir dans Kavita"
        ),
        build_request_lists_router(  # EF-USR-020: a list of wanted books, one review screen
            lambda: ops_services().request_lists
        ),
        build_tachibk_import_router(  # EF-INT-008: a Mihon backup, preview then apply
            lambda: ops_services().tachibk_import
        ),
        build_teams_router(  # EF-BIB-005: the switch to the preferred team, preview then job
            lambda: ops_services().team_switch
        ),
        build_sources_router(ops_services),  # EF-SRC-015, EF-SRC-004: search, acquire, a URL
        build_source_migration_router(ops_services),  # M4 L27: a series moves to another source
        build_me_router(cache(lambda: user_state_service(engine, clock))),  # EF-USR-007, EF-BIB-022
        build_activity_router(lambda: engine),  # EF-USR-016: `activity_view`, unmask audited
        build_catalog_lists_router(lambda: engine, since_key),  # M3 L11: `?since=`
        build_collections_router(
            lambda: engine, relations_after_audience_change
        ),  # M4 L18a: collections and reading lists
        build_selections_router(lambda: engine, clock.now),  # M5 L21: saved selections
        build_relations_router(lambda: engine, clock.now),  # M4 L19: related Works
        build_stream_router(
            lambda: engine,
            lambda: composition.bus,
            clock,
            since_key,
            max_per_profile=resolved.stream_max_per_profile,
        ),  # M3 L12 (EF-INT-002): typed SSE, one filter per reader
        build_thumbnails_router(  # M3 L24: a cover at 160/320/640, session only, authorised first
            lambda: engine, thumbnails
        ),
        build_content_router(  # M5 L05a: the bytes of a book, by ticket, sandboxed (EF-LEC-005)
            content_access,
            allowed_origin=_origin_of(resolved.public_url) if resolved.content_origin else None,
        ),
        build_reader_router(  # M5 L06: what the reader opens; L07: where it is; L13: how it reads
            lambda: PublicationService(engine, content_access()),
            lambda: ReaderPositions(engine, content_access(), clock.now, progress_lease()),
            lambda: ReaderSettings(engine),
            lambda: ContinueReading(engine, content_access()),  # M5 L14: « Reprendre la lecture »
            lambda: ReadMarks(engine, clock.now),  # M6 L18: undo a read mark
        ),
        build_opds_router(  # M4 L48: the e-reader's feed, behind a device token (ADR-0053)
            lambda: engine,
            thumbnails,
            bundles,
            clock.now,
            page_stream,  # M5 L18: OPDS-PSE, a page at a time
            renditions,
            kepubs,
        ),
        build_lite_router(  # M5 L17a: pages with no script, behind a cookie a token's secret opened
            lambda: engine, thumbnails, bundles, kepubs
        ),
        build_kosync_router(  # M4 L47: where the e-reader is, behind the same device token
            kosync, clock.now
        ),
        build_kobo_router(kobo_sync),  # M6 L17a: the Kobo sync, behind the token in the path
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
        # ENF-OBS-002: opt-in; a key of scope `metrics` alone reads it; no title, no user in it
        build_metrics_router(lambda: engine, enabled=resolved.metrics_enabled, version=__version__),
        build_status_router(ops_services),  # EF-INT-023: what waits, with the command
        build_health_page_router(lambda: engine, resolved, clock.now),  # M3 L15: admin only
        build_ops_router(ops_services),  # EF-INT-023: the exits
        # M4 L29 (EF-INT-019, EF-INT-020): history, blocklist, storage and network; admin only
        build_ops_dashboard_router(lambda: engine, clock.now),
        build_webhooks_router(webhook_admin),  # M4 L24: signed webhooks; admin only, reserved
        build_triage_router(ops_services),  # EF-USR-015, EF-EXP-046: the two lists, the batch
        build_bulk_router(ops_services),  # M4 L28 (EF-BIB-023): preview, then one job
        build_library_router(ops_services),  # pairing, repatriation, detach, purge, probe
        # EF-SRC-013, EF-EXP-065: per-source settings (nothing secret read back), attestations
        build_plugins_router(
            lambda: SourceSettings(engine, composition.vault, now=clock.now),
            lambda: AttestationStore(engine, clock=clock),
            schema_for,
            lambda: resolved.data_dir,  # M6 L06: the guard reads the installed plugins
            lambda: engine,
            # M6 L07: the pinned plugin indexes; a refresh disables what an index revoked
            lambda: RepoStore(engine, now=clock.now, layout=Layout.of(resolved.data_dir)),
            lambda: index_fetcher(composition.network),
            lambda: installed_versions(resolved),  # M6 L09a: the lifecycle, by the plugin key
        ),
    )

    async def on_shutdown() -> None:
        await stop_isolated()
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

    async def on_startup() -> None:
        log_effective_config(resolved)
        ensure_volumes(resolved)
        runtime.acquire()  # ENF-OPS-015: first, before anything else; a second instance refuses
        try:
            check_schema_not_newer(resolved)  # ENF-OPS-002: refuse to run on a newer schema
            # EF-SRC-019: third-party code only once this process owns /data and the schema is ours
            start_isolated = await load_plugins()
            if start_isolated is not None:  # in the background: a slow companion holds nothing
                isolated_start.append(asyncio.create_task(start_isolated()))
            start_owned()
            ready.set(runtime.owns_volumes)  # waiting for a migration's lock: not ready yet
            if runtime.owns_volumes:
                health.prime()  # the first `/healthz` finds the shared status already read
            if isolated_start:  # a short grace for the plugins that answer at once
                await asyncio.wait(isolated_start, timeout=ISOLATED_START_GRACE_S)
        except BaseException:
            await stop_isolated()
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

    @asynccontextmanager
    async def background() -> AsyncIterator[None]:
        """What runs beside the HTTP server: the socket of the host CLI, and the listening to the
        hub of each Kavita (EF-EXP-034), which only adds a reason to look before the polling."""
        listeners = hub_listeners(composition, active=lambda: runtime.owns_volumes)
        async with local_cli_socket(), listeners.running():
            yield

    app = create_http_app(
        health=health,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        background=background,
        version=__version__,
        frontend_dir=resolved.frontend_dir or PACKAGED_FRONTEND,
        routers=(*routers, *export_routers),
        on_profile_change=relations_after_audience_change,
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
        device_allow_plain_http=resolved.device_allow_plain_http,
        maintenance_active=MaintenanceMode(engine, clock).probe,  # EF-INT-018
    )
    app.state.content_access = content_access()  # the reader's server side issues tickets from it
    app.state.renditions = renditions()  # the cache of renditions: one, with its thread bound
    return app


def readable_copies(engine: sa.Engine, fsops: Callable[[], Fsops]) -> ReadableCopies:
    """The archives `/content` may read (a seam: the tests of the route give their own)."""
    return ReadableCopies(engine, fsops)


def _origin_of(url: str | None) -> str | None:
    """`scheme://host[:port]` of an address: what a browser sends as `Origin`."""
    if not url:
        return None
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def app_from_env() -> HttpApplication:
    """Application factory used by uvicorn (`serve`)."""
    settings = load_settings(os.environ, db_loader=load_db_settings).values
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    return create_app(settings)
