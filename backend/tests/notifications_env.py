"""Shared set-up of the notification tests: a migrated database, a vault, a scripted channel."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.fetcher import LanTargets
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.integrations.apprise import (
    ChannelStore,
    Message,
    SendResult,
    validate_channel_url,
)
from tests.fakes_clock import ManualClock
from tests.fakes_fetcher import FakeResolver

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
PUBLIC_IP = "93.184.216.34"
TOKEN = "-".join(("tkn", "synthetic", "0123456789abcdef"))  # no literal: gitleaks
CHANNEL_URL = f"ntfy://{PUBLIC_IP}/{TOKEN}"  # a literal public IP: a name needs lan_targets


class ScriptedSend:
    """Stands for Apprise: records what would leave, and fails on demand."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Message]] = []
        self.ok = True

    def __call__(self, url: str, message: Message) -> SendResult:
        self.sent.append((url, message))
        return SendResult(ok=self.ok, code="notify.sent" if self.ok else "notify.failed")


@dataclass
class NotifyEnv:
    engine: Engine
    vault: SecretVault
    clock: ManualClock
    store: ChannelStore
    bus: EventBus
    resolver: FakeResolver

    def add_channel(
        self, name: str = "ops", url: str = CHANNEL_URL, lan: LanTargets | None = None
    ) -> None:
        checked = asyncio.run(
            validate_channel_url(url, lan=lan or LanTargets.empty(), resolver=self.resolver)
        )
        with write_transaction(self.engine) as connection:
            self.store.save(connection, name, checked)


@contextmanager
def notify_env(settings: Settings) -> Iterator[NotifyEnv]:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        clock = ManualClock(NOW)
        vault = SecretVault(Keyring.generate(NOW))
        with write_transaction(engine) as connection:
            registry.sync_registry(connection, vault.keyring)
        store = ChannelStore(vault, clock)
        resolver = FakeResolver({"ntfy.example.org": [PUBLIC_IP], "nas.lan": ["192.168.1.20"]})
        yield NotifyEnv(engine, vault, clock, store, EventBus(clock, channels=store), resolver)
    finally:
        engine.dispose()
