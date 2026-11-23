"""Harness of the tracker link (M6 L13, EF-INT-010): the real application, a real `TrackerHost`
over the same database and the same keyring, and the fake tracker of the SDK kit, reached through
the production `AuthInjectingFetcher` (a scripted fetcher, nothing else reachable).

No token is a literal: every remote token is drawn at random by the test. The fake tracker is a
first-party plugin candidate loaded in a real `PluginRegistry`, exactly as the host will load a
tracker plugin the day one exists (D-P6: M6 ships the contract, the host and the fake only).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from shelfhold_sdk.fetcher import Response
from shelfhold_sdk.testing.fakes import TRACKER_KEY, TRACKER_ORIGIN, FakeFetcher, FakeTracker

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.integrations.trackers import TrackerHost
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from tests.auth_env import ADMIN, PASSWORD, AuthEnv, auth_env
from tests.fakes_clock import ManualClock

PROFILES = "/api/v1/profiles"
REMOTE_ID = "acct-7f3d"
PULLED = {"fraction": 0.25, "at": "2026-11-20T10:00:00+00:00"}


def trackers_path(user_id: str, plugin_key: str | None = None, suffix: str = "") -> str:
    base = f"{PROFILES}/{user_id}/trackers"
    if plugin_key is not None:
        base = f"{base}/{plugin_key}"
    return base + suffix


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def tracker_fetcher() -> FakeFetcher:
    """Scripted for the fake tracker: a link, a push, a pull, and nothing else."""
    return FakeFetcher(
        responses={
            ("POST", f"{TRACKER_ORIGIN}/links"): Response(
                status=200, body=_body({"remote_id": REMOTE_ID})
            ),
            ("POST", f"{TRACKER_ORIGIN}/progress/{REMOTE_ID}"): Response(
                status=200, body=_body({})
            ),
            ("GET", f"{TRACKER_ORIGIN}/progress/{REMOTE_ID}"): Response(
                status=200, body=_body(PULLED)
            ),
        }
    )


@dataclass
class Fixture:
    api: AuthEnv
    settings: Settings
    fake: FakeTracker
    fetcher: FakeFetcher
    vault: SecretVault
    host: TrackerHost
    users: dict[str, str] = field(default_factory=dict)  # username -> id

    def auth(self) -> Any:
        return self.api.app.state.auth_gate.auth  # type: ignore[attr-defined,no-any-return]

    def add(self, username: str, role: str, **kwargs: Any) -> str:
        self.users[username] = self.auth().profile_add(username, PASSWORD, role, **kwargs)
        return self.users[username]

    def client_of(self, username: str) -> Any:
        """A session of that profile, on its own IP."""
        client = self.api.other_client(f"10.0.0.{20 + len(self.users)}")
        assert self.api.login(username, client=client).status_code == 200
        return client

    def send(self, method: str, path: str, body: Any = None, client: Any = None) -> Any:
        c = client or self.api.client
        headers = {"X-CSRF-Token": self.api.csrf(c)}
        if body is None:
            return c.request(method, path, headers=headers)
        return c.request(method, path, headers=headers, json=body)

    def link(self, username: str, token: str, client: Any = None) -> Any:
        return self.send(
            "PUT", trackers_path(self.users[username], TRACKER_KEY), {"token": token}, client
        )

    def rows(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(self.settings.database_path)) as db:
            return db.execute(sql, params).fetchall()

    def links(self) -> list[tuple[Any, ...]]:
        return self.rows(
            "SELECT user_id, plugin_key, remote_ref, push_enabled, secret_ct IS NOT NULL,"
            " token_fingerprint FROM tracker_link ORDER BY created_at, id"
        )

    def allowances(self) -> list[tuple[Any, ...]]:
        return self.rows(
            "SELECT user_id, plugin_key, allowed_by FROM tracker_allowance ORDER BY user_id"
        )

    def written(self) -> str:
        """The text of every place the application writes: the database file and its WAL."""
        raw = self.settings.database_path.read_bytes().decode("latin-1")
        wal = self.settings.database_path.with_name(self.settings.database_path.name + "-wal")
        return raw + (wal.read_bytes().decode("latin-1") if wal.exists() else "")


@contextmanager
def tracker_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Fixture]:
    migrate(settings)
    vault = SecretVault(boot_check(settings))
    fake = FakeTracker()
    fetcher = tracker_fetcher()
    registry = PluginRegistry(active_keys=(TRACKER_KEY,))
    registry.load_all(
        [PluginCandidate(key=TRACKER_KEY, origin=PluginOrigin.FIRST_PARTY, load=lambda: fake)]
    )
    clock = ManualClock(datetime(2026, 11, 20, 12, 0, tzinfo=UTC))
    engine = create_db_engine(settings.database_path)
    host = TrackerHost(
        engine,
        lambda: vault,
        clock,
        registry_of=lambda: registry,
        fetcher_for=lambda origin: fetcher,
    )
    # the real routes of the real application, with the host the test controls
    monkeypatch.setattr("shelfhold.main.TrackerHost", lambda *_a, **_k: host)
    with auth_env(monkeypatch, settings) as api:
        assert api.login(ADMIN).status_code == 200
        yield Fixture(api, settings, fake, fetcher, vault, host, users={})
    engine.dispose()
