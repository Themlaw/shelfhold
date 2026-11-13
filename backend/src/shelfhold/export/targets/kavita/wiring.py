"""The composition root of the Kavita client: the write guard is always posed (L26, L29).

`target_write_guard` makes every write route answer `DestinationPausedError` while a shelf of the
target is paused (child exposure, reinstalled server, 403). A client built anywhere else would
write through a pause; every client of the application is built here.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.roots import load_roots, open_fsops
from shelfhold.export.application.target_state import load_health, target_write_guard
from shelfhold.export.domain.path_map import PathMap, PathMode
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.export.targets.kavita.adopt import Adopter, AdoptionSession
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.credentials import AdminKey, load_admin_key
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher import GuardedFetcher, Network, Resolver
from shelfhold.infrastructure.schema import export, platform
from shelfhold.infrastructure.secrets import SecretVault, diagnostics
from shelfhold.library.public import TombstoneRoot, TombstoneService

__all__ = [
    "TargetUrlRefused",
    "checked_target_url",
    "guarded_client",
    "open_adoption",
    "open_client",
]


def guarded_client(client: KavitaClient, engine: Engine, target_id: str) -> KavitaClient:
    client.set_write_guard(target_write_guard(engine, target_id))
    client.bind_target(target_id)
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


class TargetUrlRefused(ValueError):
    """The address of a Kavita target is not one Shelfhold may send its key to."""


async def checked_target_url(url: str, resolver: Resolver) -> tuple[str, int]:
    """`(host, port)` of `export_target.config["url"]`, or `TargetUrlRefused`.

    The shape is that of the headless configuration (http(s)://host[:port], no credentials, no
    query). A Kavita is a server of the owner's network: a public address (literal, or a name
    that resolves to one) is refused, and so is plain http to anything that is not LAN.
    """
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"} or not parts.hostname
        or parts.username is not None or parts.password is not None
        or parts.query or parts.fragment
    ):  # fmt: skip
        raise TargetUrlRefused("the address must be http(s)://host[:port] without credentials")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        found = await resolver.resolve(parts.hostname, port)
    except Exception as error:
        raise TargetUrlRefused("the address does not resolve") from error
    addresses = [ipaddress.ip_address(a.split("%")[0]) for a in found]
    if not addresses:
        raise TargetUrlRefused("the address does not resolve")
    public = [a for a in addresses if a.is_global]
    if public:
        raise TargetUrlRefused("a public address is refused: a Kavita is on the local network")
    if parts.scheme != "https" and not all(a.is_private or a.is_loopback for a in addresses):
        raise TargetUrlRefused("plain http is for the local network only")
    return parts.hostname, port


def open_adoption(
    settings: Settings,
    destination_id: str,
    *,
    bus: EventBus,
    events_for: Callable[[Engine], TargetEvents],
    network: Network | None = None,
) -> AdoptionSession:
    """The adopter and the Kavita client of a destination's target (`shelfhold adopt`).

    The target's address is `export_target.config["url"]`; its host:port is the one `lan_target`
    the fetcher may reach, pinned to what the name resolves to now. The key is opened from the
    sealed row; the client carries the write guard (`guarded_client`). The operating events go to
    the bus of the application (`events_for` builds the sink over the engine opened here).
    """
    network = network or Network()
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
    asyncio.run(checked_target_url(url, network.resolver))
    clock = SystemClock()
    events = events_for(engine)
    keyring = diagnostics.boot_check(settings, create=False, announce=False)
    vault = SecretVault(keyring)
    client = open_client(
        engine, vault, network.integration_fetcher(url), target_id=row.id,
        base_url=url, events=events, clock=clock,
    )  # fmt: skip
    fsops = open_fsops(loaded, umask=settings.umask)
    tombstones = TombstoneService(engine, keyring, fsops, bus)
    tombstones.replay(
        [
            TombstoneRoot(r.root_id, r.path, mounted=fsops.root_state(r.path) is RootState.OK)
            for r in loaded.roots
            if r.mode is PathMode.RW
        ]
    )  # membership fails closed until a replay ran: a purged file is not adopted again
    adopter = Adopter(
        engine, fsops, locate=loaded.locate, clock=clock,
        auditor=AccessAuditor(engine, clock, events), tombstones=tombstones,
    )  # fmt: skip
    return AdoptionSession(adopter, client)
