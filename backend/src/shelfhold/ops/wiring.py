"""The operating services over a real instance (`shelfhold status`, the exit commands, the API).

Everything that needs the keyring or the network is opened at its first use: `status` reads the
database and never creates a keyring. A component that another lot has not connected yet is
`None` here and its exit answers `ops.exit_unavailable`.
"""

from __future__ import annotations

from functools import cache

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.export.application.roots import load_roots
from shelfhold.export.domain.path_map import PathMap
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.reinstall import InstanceService
from shelfhold.export.targets.kavita.wiring import open_client
from shelfhold.headless import HeadlessAdmin
from shelfhold.headless import wiring as headless_wiring
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import export, platform
from shelfhold.infrastructure.secrets import SecretVault, diagnostics
from shelfhold.ops.events import BusTargetEvents
from shelfhold.ops.kavita import KavitaTargetOps
from shelfhold.ops.keyring import TombstoneKeyringExit
from shelfhold.ops.services import OpsServices

__all__ = ["open_ops"]


class _LazyHeadless:
    """The configuration facade, opened when a credential is renewed (it may create the keyring)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @cache  # noqa: B019 (one instance per process)
    def _admin(self) -> HeadlessAdmin:
        return headless_wiring.open_headless(self._settings)

    def target_rotate_key(self, *, target: str, password: str) -> object:
        return self._admin().target_rotate_key(target=target, password=password)

    def source_set_secret(self, secret: str) -> object:
        return self._admin().source_set_secret(secret)

    def source_recheck(self) -> object:
        return self._admin().source_recheck()


def _fsops(engine: Engine) -> Fsops | None:
    with engine.connect() as connection:
        paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
        if not paths:
            return None
        loaded = load_roots(connection, PathMap.same_path(paths))
    return Fsops.open(loaded.path_map, loaded.expected)


def _client_for(settings: Settings, engine: Engine, clock: Clock, events: BusTargetEvents):  # type: ignore[no-untyped-def]
    def build(target_id: str) -> KavitaClient:
        with engine.connect() as connection:
            url = str(
                connection.execute(
                    sa.select(export.export_target.c.config).where(
                        export.export_target.c.id == target_id
                    )
                ).scalar_one()["url"]
            )
        vault = SecretVault(diagnostics.boot_check(settings, create=False, announce=False))
        return open_client(
            engine, vault, headless_wiring.integration_fetcher(url), target_id=target_id,
            base_url=url, events=events, clock=clock,
        )  # fmt: skip

    return build


def open_ops(
    settings: Settings, *, engine: Engine | None = None, mount: bool = True
) -> OpsServices:
    """`mount=False`: the library is not opened (a read of the database, as `status` does)."""
    engine = engine or create_db_engine(settings.database_path)
    clock = SystemClock()
    bus = EventBus(clock)
    events = BusTargetEvents(engine, bus)
    targets = KavitaTargetOps(
        engine, clock=clock, client_for=_client_for(settings, engine, clock, events),
        instance=InstanceService(engine, clock, events),
        auditor=AccessAuditor(engine, clock, events),
    )  # fmt: skip
    return OpsServices(
        engine, clock=clock, bus=bus, fsops=_fsops(engine) if mount else None, targets=targets,
        headless=_LazyHeadless(settings), keyring=TombstoneKeyringExit(settings, engine, clock),
    )  # fmt: skip
