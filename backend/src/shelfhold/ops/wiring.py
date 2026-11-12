"""The operating services over a real instance (`shelfhold status`, the exit commands, the API).

Everything that needs the keyring or the network is opened at its first use: `status` reads the
database and never creates a keyring. A component that another lot has not connected yet is
`None` here and its exit answers `ops.exit_unavailable`.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.composition import Composition, open_fsops
from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.export.application.roots import load_roots
from shelfhold.export.domain.path_map import PathMap
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.reinstall import InstanceService
from shelfhold.headless import HeadlessAdmin
from shelfhold.headless import wiring as headless_wiring
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import platform
from shelfhold.jobs.control import QueueControl
from shelfhold.jobs.window import MaintenanceWindow
from shelfhold.ops.acquire import AcquireOps
from shelfhold.ops.adoption import LibraryAdoptionExit
from shelfhold.ops.attach import AttachOps
from shelfhold.ops.backfill import BackfillOps
from shelfhold.ops.content import ContentOps
from shelfhold.ops.contributors import ContributorOps
from shelfhold.ops.dedup import DedupOps
from shelfhold.ops.edition_merge import EditionMergeOps
from shelfhold.ops.editions import EditionOps
from shelfhold.ops.events import BusTargetEvents
from shelfhold.ops.export_all import ExportAllOps
from shelfhold.ops.export_item import ExportItemOps
from shelfhold.ops.follows import FollowOps
from shelfhold.ops.import_folder import ImportOps
from shelfhold.ops.intents import IntentOps
from shelfhold.ops.kavita import KavitaTargetOps
from shelfhold.ops.keyring import TombstoneKeyringExit
from shelfhold.ops.library import LibraryOps
from shelfhold.ops.maintenance import MaintenanceOps
from shelfhold.ops.meta import MetaOps
from shelfhold.ops.normalize import NormalizeOps
from shelfhold.ops.rating import RatingOps
from shelfhold.ops.requests import build_service
from shelfhold.ops.resplit import ResplitOps
from shelfhold.ops.restore import RestoreCredentialsExit, RestoreGuardExit
from shelfhold.ops.revisions import RevisionOps
from shelfhold.ops.roots import RootOps
from shelfhold.ops.services import OpsServices
from shelfhold.ops.tombstone_screens import CatalogScreens

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

    def source_reconnect(self) -> object:
        return self._admin().source_reconnect()


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


def _live_fsops(
    engine: Engine, umask: int, clock: Clock, bus: EventBus
) -> Callable[[], Fsops | None]:
    def current() -> Fsops | None:
        fsops = _fsops(engine, umask)
        if fsops is not None and RestoreGuardExit(engine, lambda: fsops, clock, bus).waiting():
            fsops.freeze()  # restored behind: this process writes nothing under /library either
        return fsops

    return current


def open_ops(
    settings: Settings,
    *,
    engine: Engine | None = None,
    mount: bool = True,
    composition: Composition | None = None,
    revoke: Callable[[str], None] | None = None,
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
    # The roots are read when an exit runs, not when this object is built: `target add` registers
    # a root while the server runs, and `series resume` / `target recheck` must see it.
    live = _live_fsops(engine, settings.umask, clock, bus) if mount else None

    # the restore guard only reads the sentinels against the database: `status` lists
    # `restored_behind` without mounting the library for writing
    def read_only() -> Fsops | None:
        return _fsops(engine, settings.umask)

    library = LibraryOps(composition)
    window = MaintenanceWindow.from_text(
        settings.maintenance_window_start, settings.maintenance_window_end, settings.timezone
    )
    return OpsServices(
        engine, clock=clock, bus=bus, fsops=live, targets=targets,
        headless=_LazyHeadless(settings),
        keyring=TombstoneKeyringExit(settings, engine, clock, composition),
        restore=RestoreGuardExit(engine, read_only, clock, bus),
        credentials=RestoreCredentialsExit(engine, clock),
        library=library, adoption=LibraryAdoptionExit(engine, library),
        intents=IntentOps(composition), revisions=RevisionOps(composition),
        backfill=BackfillOps(composition), import_folder=ImportOps(composition),
        normalize=NormalizeOps(composition), meta=MetaOps(composition),
        content=ContentOps(composition),
        rating=RatingOps(engine, clock, fresh_auth_s=settings.fresh_auth_s),
        attach=AttachOps(
            engine, clock, bus, fresh_auth_s=settings.fresh_auth_s,
            screens=CatalogScreens(composition),
        ),
        edition_merge=EditionMergeOps(composition), editions=EditionOps(composition),
        dedup=DedupOps(engine, CatalogScreens(composition)),
        resplit=ResplitOps(composition), contributors=ContributorOps(composition),
        export_items=ExportItemOps(composition), export_all=ExportAllOps(composition),
        follows=FollowOps(composition), acquire=AcquireOps(composition),
        requests=build_service(composition), roots=RootOps(composition),
        maintenance=MaintenanceOps(
            engine, clock, fresh_auth_s=settings.fresh_auth_s, hardened=settings.auth_hardened,
            window=window,
            control=QueueControl(revoke=revoke),
        ),
        window=window,
    )  # fmt: skip
