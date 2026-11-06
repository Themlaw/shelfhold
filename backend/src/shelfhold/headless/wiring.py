"""The facade over a real instance: the database, the keyring, the guarded Fetcher."""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import Engine

from shelfhold.composition import Composition
from shelfhold.config import Settings, load_settings
from shelfhold.export.application.preflight import FolderProbe
from shelfhold.export.application.root_bootstrap import RootPolicy
from shelfhold.export.targets.kavita.adopt import AdoptionRefused
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.linking import ShelfLinker
from shelfhold.headless.adapters import HttpKavitaKeyExchange
from shelfhold.headless.admin import HeadlessAdmin
from shelfhold.headless.errors import ShelfExposedToChildren
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher import (
    GuardedFetcher,
    LanTargets,
    Network,
    SystemResolver,
)
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.infrastructure.settings_store import load_db_settings
from shelfhold.integrations.apprise.destination import ChannelUrlRefused, validate_channel_url
from shelfhold.ops.events import BusTargetEvents
from shelfhold.plugin_host.adapters.meta.suwayomi.probe import HttpSuwayomiProbe

__all__ = ["integration_fetcher", "open_headless"]


def integration_fetcher(url: str) -> GuardedFetcher:
    """The Fetcher for the address an admin typed (see `Network.integration_fetcher`)."""
    return Network().integration_fetcher(url)


class _Channels:
    def validate(self, url: str) -> str:
        try:
            return asyncio.run(
                validate_channel_url(url, lan=LanTargets.empty(), resolver=SystemResolver())
            ).url
        except ChannelUrlRefused:
            raise ValueError("refused") from None


class _ShelfLinker:
    """Links a new destination to a Kavita library with the operating client of the target."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self._comp = Composition(settings, engine, clock=SystemClock())

    def link(self, target_id: str, destination_id: str, *, library_name: str) -> str:
        comp = self._comp
        client = comp.kavita_client(target_id)
        linker = ShelfLinker(
            comp.engine, comp.clock,
            AccessAuditor(comp.engine, comp.clock, BusTargetEvents(comp.engine, comp.bus)),
        )  # fmt: skip
        try:
            return asyncio.run(linker.link(client, destination_id, library_name=library_name))
        except AdoptionRefused as refusal:
            raise ShelfExposedToChildren(refusal.accounts) from None

    def make_folder(self, local: str) -> None:
        with self._comp.write_context() as w:
            FolderProbe(w.fsops).make_folder(local)


def open_headless(settings: Settings) -> HeadlessAdmin:
    engine = create_db_engine(settings.database_path)
    vault = SecretVault(boot_check(settings, announce=False))  # first use creates the keyring
    return HeadlessAdmin(
        engine,
        vault,
        clock=SystemClock(),
        kavita=HttpKavitaKeyExchange(integration_fetcher),
        suwayomi=HttpSuwayomiProbe(integration_fetcher),
        channels=_Channels(),
        resolve=lambda: load_settings(os.environ, db_loader=load_db_settings),
        roots=RootPolicy(settings.library_dir, settings.data_dir, settings.store_dir),
        linker=_ShelfLinker(settings, engine),
    )
