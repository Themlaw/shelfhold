"""Harness of the headless-configuration tests: a migrated database, a real vault, the two network
ports replaced by recorders (nothing leaves the test)."""
# ruff: noqa: S105 (synthetic secrets; the tables of arguments read better long)

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.headless import ExchangedKey, HeadlessAdmin
from shelfhold.headless.wiring import shelf_linker
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault
from tests.fakes_clock import ManualClock

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
KAVITA_KEY = "-".join(("synthetic", "kavita", "key", "0038"))  # no literal: gitleaks
KAVITA_PASSWORD = "synthetic-kavita-password-0038"
SUWAYOMI_PASSWORD = "synthetic-suwayomi-password-0038"
KAVITA_URL = "http://kavita.internal:5000"
SUWAYOMI_URL = "http://suwayomi.internal:4567"


@dataclass
class FakeKavitaExchange:
    """The `KavitaKeyExchange` port: records what it was asked, hands out one synthetic key."""

    calls: list[dict[str, object]] = field(default_factory=list)
    revoked: list[dict[str, object]] = field(default_factory=list)

    def exchange(
        self, *, url: str, username: str, password: str, key_name: str, expires_days: int
    ) -> ExchangedKey:
        self.calls.append(
            {"url": url, "username": username, "password": password, "key_name": key_name,
             "expires_days": expires_days}
        )  # fmt: skip
        return ExchangedKey(KAVITA_KEY, NOW + timedelta(days=expires_days), "0.9.1.4")

    def revoke(self, *, url: str, username: str, password: str, key: str, key_name: str) -> bool:
        self.revoked.append({"url": url, "key": key, "key_name": key_name, "password": password})
        return True


@dataclass
class FakeSuwayomiProbe:
    """The `SuwayomiProbe` port: answers the authentication mode it was told to."""

    mode: str = "basic_auth"
    roles_verified: bool = True
    blocking_filters: list[str] = field(default_factory=list)  # global update filters (L22)
    calls: list[dict[str, object]] = field(default_factory=list)

    def auth_mode(self, *, url: str, username: str | None, password: str | None) -> str:
        self.calls.append({"url": url, "username": username, "password": password})
        return self.mode


@dataclass
class FakeChannels:
    """The notification URL validator: accepts `ntfy://` only, like a short allow-list."""

    checked: list[str] = field(default_factory=list)

    def validate(self, url: str) -> str:
        self.checked.append(url)
        if not url.startswith("ntfy://"):
            raise ValueError("scheme_not_allowed")
        return url


@dataclass
class Headless:
    admin: HeadlessAdmin
    engine: Engine
    settings: Settings
    kavita: FakeKavitaExchange
    suwayomi: FakeSuwayomiProbe
    channels: FakeChannels
    vault: SecretVault
    clock: ManualClock

    def count(self, table: str) -> int:
        with self.engine.connect() as connection:
            return int(connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608

    def dump(self) -> str:
        """The whole database as text, for "this secret is nowhere"."""
        raw = self.engine.raw_connection()
        try:
            return "\n".join(raw.driver_connection.iterdump())  # type: ignore[attr-defined]
        finally:
            raw.close()


@contextmanager
def headless(
    settings: Settings, *, real_linker: bool = False, **options: object
) -> Iterator[Headless]:
    """`suwayomi=` replaces the probe; `real_linker` plugs the production one (the disk part)."""
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    vault = SecretVault(Keyring.generate(NOW))
    kavita, suwayomi, channels = FakeKavitaExchange(), FakeSuwayomiProbe(), FakeChannels()
    clock = ManualClock(NOW)
    probe = options.pop("suwayomi", suwayomi)
    if real_linker:
        options["linker"] = shelf_linker(settings, engine)
    admin = HeadlessAdmin(
        engine,
        vault,
        clock=clock,
        kavita=kavita,
        suwayomi=probe,  # type: ignore[arg-type]
        channels=channels,
        **options,  # type: ignore[arg-type]
    )
    try:
        yield Headless(admin, engine, settings, kavita, suwayomi, channels, vault, clock)
    finally:
        engine.dispose()


def write_secret(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path
