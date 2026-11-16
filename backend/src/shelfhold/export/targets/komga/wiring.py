"""The composition root of the Komga client: the write guard is always posed.

`target_write_guard` makes every write route answer `DestinationPausedError` while a shelf of the
target is paused (child exposure, reinstalled server, 403). A client built anywhere else would
scan through a pause; every Komga client of the application is built here.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import Engine

from shelfhold.export.application.target_state import target_write_guard
from shelfhold.export.domain.scan_debounce import DEFAULT_QUIET
from shelfhold.export.targets.komga.client import KomgaClient
from shelfhold.export.targets.komga.credentials import load_komga_key
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fetcher import GuardedFetcher
from shelfhold.infrastructure.secrets import SecretVault

__all__ = ["open_client"]


def open_client(
    engine: Engine,
    vault: SecretVault,
    fetcher: GuardedFetcher,
    *,
    target_id: str,
    base_url: str,
    clock: Clock,
    scan_quiet: timedelta = DEFAULT_QUIET,
    **_options: Any,
) -> KomgaClient:
    """The client of a target: its sealed key, its write guard."""
    with engine.connect() as connection:
        key = load_komga_key(vault, connection, target_id)
    client = KomgaClient(fetcher, base_url, key, clock=clock, scan_quiet=scan_quiet)
    client.set_write_guard(target_write_guard(engine, target_id))
    return client
