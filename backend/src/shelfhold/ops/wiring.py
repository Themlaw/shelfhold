"""The operating services over a real instance (`shelfhold status`, the exit commands, the API).

Everything that needs the keyring or the network is opened at its first use: `status` reads the
database and never creates a keyring. A component that another lot has not connected yet is
`None` here and its exit answers `ops.exit_unavailable`.
"""

from __future__ import annotations

from functools import cache

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.composition import Composition, open_fsops
from shelfhold.config import Settings
from shelfhold.export.application.roots import load_roots
from shelfhold.export.domain.path_map import PathMap
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.reinstall import InstanceService
from shelfhold.headless import HeadlessAdmin
from shelfhold.headless import wiring as headless_wiring
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import platform
from shelfhold.ops.adoption import LibraryAdoptionExit
from shelfhold.ops.backfill import BackfillOps
from shelfhold.ops.events import BusTargetEvents
from shelfhold.ops.intents import IntentOps
from shelfhold.ops.kavita import KavitaTargetOps
from shelfhold.ops.keyring import TombstoneKeyringExit
from shelfhold.ops.library import LibraryOps
from shelfhold.ops.meta import MetaOps
from shelfhold.ops.normalize import NormalizeOps
from shelfhold.ops.restore import RestoreGuardExit
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


def _fsops(engine: Engine, umask: int) -> Fsops | None:
    try:
        with engine.connect() as connection:
            paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
            if not paths:
                return None
            loaded = load_roots(connection, PathMap.same_path(paths))
    except sa.exc.OperationalError:  # not migrated yet: nothing is mounted
        return None
    return open_fsops(loaded, umask=umask)


def open_ops(
    settings: Settings,
    *,
    engine: Engine | None = None,
    mount: bool = True,
    composition: Composition | None = None,
) -> OpsServices:
    """`mount=False`: the library is not opened (a read of the database, as `status` does).
    `composition`: the one of the running server; a command run by itself builds its own."""
    engine = engine or (
        composition.engine if composition else create_db_engine(settings.database_path)
    )
    composition = composition or Composition(settings, engine)
    clock, bus = composition.clock, composition.bus
    events = BusTargetEvents(engine, bus)
    targets = KavitaTargetOps(
        engine, clock=clock, client_for=composition.kavita_client,
        instance=InstanceService(engine, clock, events),
        auditor=AccessAuditor(engine, clock, events),
    )  # fmt: skip
    fsops = _fsops(engine, settings.umask) if mount else None
    # the restore guard only reads the sentinels against the database: `status` lists
    # `restored_behind` without mounting the library for writing
    guard_fsops = fsops if fsops is not None else _fsops(engine, settings.umask)
    if mount and fsops is not None and RestoreGuardExit(engine, fsops, clock, bus).waiting():
        fsops.freeze()  # restored behind: this process writes nothing under /library either
    library = LibraryOps(composition)
    return OpsServices(
        engine, clock=clock, bus=bus, fsops=fsops, targets=targets,
        headless=_LazyHeadless(settings), keyring=TombstoneKeyringExit(settings, engine, clock),
        restore=RestoreGuardExit(engine, guard_fsops, clock, bus) if guard_fsops else None,
        library=library, adoption=LibraryAdoptionExit(engine, library),
        intents=IntentOps(composition), backfill=BackfillOps(composition),
        normalize=NormalizeOps(composition), meta=MetaOps(composition),
    )  # fmt: skip
