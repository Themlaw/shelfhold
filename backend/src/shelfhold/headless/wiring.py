"""The facade over a real instance: the database, the keyring, the guarded Fetcher."""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

from shelfhold.config import Settings, load_settings
from shelfhold.headless.adapters import HttpKavitaKeyExchange
from shelfhold.headless.admin import HeadlessAdmin
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher import (
    FetchError,
    GuardedFetcher,
    IntegrationFetcher,
    InvalidLanTarget,
    LanTargets,
    SystemResolver,
)
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.infrastructure.settings_store import load_db_settings
from shelfhold.integrations.apprise.destination import ChannelUrlRefused, validate_channel_url
from shelfhold.plugin_host.adapters.meta.suwayomi.probe import HttpSuwayomiProbe

__all__ = ["integration_fetcher", "open_headless"]


def integration_fetcher(url: str) -> GuardedFetcher:
    """The Fetcher for the address an admin typed: that exact host and port is declared a LAN
    target (explicit `host:port`, name pinned to its addresses); anything else stays guarded."""
    parts = urlsplit(url)
    resolver = SystemResolver()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        lan = asyncio.run(LanTargets.declare([f"{parts.hostname}:{port}"], resolver))
    except (InvalidLanTarget, FetchError):  # not a LAN address, or blocked: stay guarded
        return GuardedFetcher(resolver)
    return IntegrationFetcher(resolver, lan_targets=lan)


class _Channels:
    def validate(self, url: str) -> str:
        try:
            return asyncio.run(
                validate_channel_url(url, lan=LanTargets.empty(), resolver=SystemResolver())
            ).url
        except ChannelUrlRefused:
            raise ValueError("refused") from None


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
    )
