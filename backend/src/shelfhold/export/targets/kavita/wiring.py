"""The composition root of the Kavita client: the write guard is always posed (L26, L29).

`target_write_guard` makes every write route answer `DestinationPausedError` while a shelf of the
target is paused (child exposure, reinstalled server, 403). A client built anywhere else would
write through a pause; every client of the application is built here.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.roots import load_roots
from shelfhold.export.application.target_state import load_health, target_write_guard
from shelfhold.export.domain.path_map import PathMap
from shelfhold.export.targets.kavita.adopt import Adopter, AdoptionSession
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.credentials import AdminKey, load_admin_key
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher import (
    GuardedFetcher,
    IntegrationFetcher,
    LanTargets,
    SystemResolver,
)
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import export, platform
from shelfhold.infrastructure.secrets import SecretVault, diagnostics

__all__ = ["guarded_client", "open_adoption", "open_client"]


def guarded_client(client: KavitaClient, engine: Engine, target_id: str) -> KavitaClient:
    client.set_write_guard(target_write_guard(engine, target_id))
    return client


def open_client(
    engine: Engine,
    vault: SecretVault,
    fetcher: GuardedFetcher,
    *,
    target_id: str,
    base_url: str,
    events: TargetEvents,
    clock: Clock,
    **options: Any,
) -> KavitaClient:
    """The client of a target: its sealed key, its stored health, its write guard."""
    with engine.connect() as connection:
        key: AdminKey = load_admin_key(vault, connection, target_id)
        health = load_health(connection, target_id)
    client = KavitaClient(
        fetcher, base_url, key, events=events, clock=clock, health=health, **options
    )
    return guarded_client(client, engine, target_id)


class _LogEvents:
    """The operating events of the Kavita destination, as log lines (the bus joins at L36)."""

    def emit(self, code: str, **detail: object) -> None:
        structlog.get_logger("shelfhold.export").warning(code, **detail)


def open_adoption(settings: Settings, destination_id: str) -> AdoptionSession:
    """The adopter and the Kavita client of a destination's target (`shelfhold adopt`).

    The target's address is `export_target.config["url"]`; its host:port is the one `lan_target`
    the fetcher may reach, pinned to what the name resolves to now. The key is opened from the
    sealed row; the client carries the write guard (`guarded_client`).
    """
    engine = create_db_engine(settings.database_path)
    with engine.connect() as connection:
        paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
        loaded = load_roots(connection, PathMap.same_path(paths))
        row = connection.execute(
            sa.select(export.export_target.c.id, export.export_target.c.config)
            .select_from(
                export.shelf_destination.join(
                    export.export_target,
                    export.export_target.c.id == export.shelf_destination.c.target_id,
                )
            )
            .where(export.shelf_destination.c.id == destination_id)
        ).one()
    url = str(row.config.get("url", ""))
    parts = urlsplit(url)
    if parts.hostname is None:
        raise ValueError("the Kavita target has no `url` in its configuration")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    resolver = SystemResolver()
    lan = asyncio.run(LanTargets.declare([f"{parts.hostname}:{port}"], resolver))
    clock = SystemClock()
    events = _LogEvents()
    vault = SecretVault(diagnostics.boot_check(settings, create=False, announce=False))
    client = open_client(
        engine, vault, IntegrationFetcher(resolver, lan_targets=lan), target_id=row.id,
        base_url=url, events=events, clock=clock,
    )  # fmt: skip
    fsops = Fsops.open(loaded.path_map, loaded.expected)
    adopter = Adopter(
        engine, fsops, locate=loaded.locate, clock=clock,
        auditor=AccessAuditor(engine, clock, events),
    )  # fmt: skip
    return AdoptionSession(adopter, client)
