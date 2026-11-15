"""Harness of the member Kavita account link (M4 L12, EF-USR-012).

The real application (`create_app`), a real `DestinationAccounts` service over the same database
and the same keyring, and a simulated Kavita reached through the production `IntegrationFetcher`
(declared LAN target, scripted DNS, mock transport). No key is a literal: every member's key is
drawn at random by the simulated server.
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

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.identity.public import LocalAuth
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.ops import OpsServices
from shelfhold.ops.destination_accounts import open_destination_accounts
from tests.auth_env import ADMIN, CSRF_HEADER, PASSWORD, AuthEnv, auth_env
from tests.fakes.kavita import BASE_URL, FakeKavita, build_fetcher
from tests.fakes_clock import ManualClock
from tests.integration.test_schema_migration_0046 import add_row

ULID_UNKNOWN = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
PROFILES = "/api/v1/profiles"


def accounts_path(user_id: str, destination_id: str | None = None) -> str:
    base = f"{PROFILES}/{user_id}/destination-accounts"
    return base if destination_id is None else f"{base}/{destination_id}"


@dataclass
class Fixture:
    api: AuthEnv
    settings: Settings
    fake: FakeKavita
    vault: SecretVault
    service: Any
    clock: ManualClock
    destination_id: str  # a binding to the simulated Kavita
    other_destination_id: str  # a second binding, to the same Kavita
    foreign_destination_id: str  # a binding to a target that is not a Kavita
    users: dict[str, str] = field(default_factory=dict)  # username -> id
    fetches: list[str] = field(default_factory=list)  # the base URLs a fetcher was asked for

    def auth(self) -> LocalAuth:
        return self.api.app.state.auth_gate.auth  # type: ignore[attr-defined,no-any-return]

    def add(self, username: str, role: str, **kwargs: Any) -> str:
        self.users[username] = self.auth().profile_add(username, PASSWORD, role, **kwargs)
        return self.users[username]

    def as_user(self, username: str) -> None:
        self.api.client.cookies.clear()
        assert self.api.login(username).status_code == 200

    def send(self, method: str, path: str, body: Any = None) -> Any:
        headers = {CSRF_HEADER: self.api.csrf()}
        if body is None:
            return self.api.client.request(method, path, headers=headers)
        return self.api.client.request(method, path, headers=headers, json=body)

    def put(
        self, user_id: str, destination_id: str, token: str, remote_user: str | None = None
    ) -> Any:
        body: dict[str, Any] = {"token": token}
        if remote_user is not None:
            body["remote_user"] = remote_user
        return self.send("PUT", accounts_path(user_id, destination_id), body)

    def rows(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(self.settings.database_path)) as db:
            return db.execute(sql, params).fetchall()

    def accounts(self) -> list[tuple[Any, ...]]:
        return self.rows(
            "SELECT user_id, shelf_destination_id, remote_user, token_fingerprint, "
            "revoked_at IS NOT NULL FROM destination_account ORDER BY created_at, id"
        )

    def audit(self, like: str = "destination_account.%") -> list[tuple[Any, ...]]:
        return self.rows("SELECT action, target FROM audit WHERE action LIKE ? ORDER BY id", like)


def _seed_destinations(settings: Settings) -> tuple[str, str, str]:
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        kavita, other = new_ulid(), new_ulid()
        add_row(db, "export_target", id=kavita, kind="kavita", name="kavita-main",
                config=json.dumps({"url": BASE_URL}))  # fmt: skip
        add_row(db, "export_target", id=other, kind="folder", name="plain-folder")
        shelves = []
        for name in ("Manga", "Comics"):
            shelf_id = new_ulid()
            add_row(db, "shelf", id=shelf_id, name=f"s-{shelf_id}", phrase=name)
            shelves.append(shelf_id)

        def destination(shelf_id: str, target_id: str, subpath: str) -> str:
            destination_id = new_ulid()
            add_row(db, "shelf_destination", id=destination_id, shelf_id=shelf_id,
                    target_id=target_id, transport="tree", egress="foyer", root_subpath=subpath,
                    template="{series}", template_origin="default@1")  # fmt: skip
            return destination_id

        return (
            destination(shelves[0], kavita, "manga"),
            destination(shelves[1], kavita, "comics"),
            destination(shelves[0], other, "plain"),
        )


@contextmanager
def accounts_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Fixture]:
    migrate(settings)
    destination, other, foreign = _seed_destinations(settings)
    vault = SecretVault(boot_check(settings))
    fake = FakeKavita()
    fetcher = build_fetcher(fake)
    clock = ManualClock(datetime(2026, 11, 15, 12, 0, tzinfo=UTC))
    engine = create_db_engine(settings.database_path)
    fetches: list[str] = []

    def fetcher_for(url: str) -> Any:
        fetches.append(url)
        return fetcher

    service = open_destination_accounts(engine, vault, clock=clock, fetcher_for=fetcher_for)
    ops = OpsServices(engine, clock=clock, bus=EventBus(clock), destination_accounts=service)
    with auth_env(monkeypatch, settings, ops_services=ops) as api:
        assert api.login(ADMIN).status_code == 200
        yield Fixture(api, settings, fake, vault, service, clock, destination, other, foreign,
                      fetches=fetches)  # fmt: skip
    engine.dispose()
