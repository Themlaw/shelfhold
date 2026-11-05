"""The real application (`create_app`) over a migrated database, with a hand-moved clock.

The admin is created through the CLI, as an operator would; requests go through the full
middleware stack (discretion headers, request id, authentication, CSRF, idempotency).
"""

import io
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.interfaces.cli.registry import main as cli_main
from shelfhold.main import create_app

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
ADMIN = "admin"
PASSWORD = "correct horse battery staple"  # noqa: S105 (test fixture)
WRONG = "wrong password!"
HTTPS = "https://testserver"
SESSION_COOKIE = "__Host-sid"
CSRF_COOKIE = "__Host-csrf"
CSRF_HEADER = "X-CSRF-Token"


@dataclass
class ManualClock:
    current: datetime = T0

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def run_cli(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, *argv: str, stdin: str | None = None
) -> int:
    monkeypatch.setenv("SHELFHOLD_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("SHELFHOLD_STORE_DIR", str(settings.store_dir))
    if settings.auth_hardened:
        monkeypatch.setenv("SHELFHOLD_AUTH_HARDENED", "true")
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return cli_main(list(argv))


def create_admin(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    *,
    username: str = ADMIN,
    password: str = PASSWORD,
) -> int:
    secret = settings.data_dir.parent / f"{username}.password"
    secret.write_text(password + "\n", encoding="utf-8")
    secret.chmod(0o600)
    return run_cli(
        monkeypatch, settings, "admin", "create", "--username", username, "--password-file",
        str(secret),
    )  # fmt: skip


def db_dump(settings: Settings) -> str:
    connection = sqlite3.connect(settings.database_path)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


def db_rows(settings: Settings, sql: str, *params: object) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(settings.database_path)
    try:
        return list(connection.execute(sql, params))
    finally:
        connection.close()


@dataclass
class AuthEnv:
    settings: Settings
    clock: ManualClock
    client: TestClient
    app: object = None
    calls: list[str] = field(default_factory=list)

    def login(
        self,
        username: str = ADMIN,
        password: str = PASSWORD,
        *,
        client: TestClient | None = None,
        **extra: object,
    ) -> httpx.Response:
        body: dict[str, object] = {"username": username, "password": password, **extra}
        return (client or self.client).post("/api/v1/auth/login", json=body)

    def csrf(self, client: TestClient | None = None) -> str:
        value = (client or self.client).cookies.get(CSRF_COOKIE)
        assert value, "no CSRF cookie: not logged in"
        return value

    def post(
        self, path: str, *, client: TestClient | None = None, **kwargs: object
    ) -> httpx.Response:
        c = client or self.client
        headers = dict(kwargs.pop("headers", None) or {})  # type: ignore[call-overload]
        headers.setdefault(CSRF_HEADER, self.csrf(c))
        return c.post(path, headers=headers, **kwargs)  # type: ignore[arg-type]

    def other_client(self, ip: str, base_url: str = HTTPS) -> TestClient:
        return TestClient(self.app, base_url=base_url, client=(ip, 40000))  # type: ignore[arg-type]


@contextmanager
def auth_env(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    *,
    routers: Sequence[APIRouter] = (),
    base_url: str = HTTPS,
    with_admin: bool = True,
    hardened: bool = False,
) -> Iterator[AuthEnv]:
    if hardened:
        settings = settings.model_copy(update={"auth_hardened": True})
    migrate(settings)
    if with_admin:
        assert create_admin(monkeypatch, settings) == 0
    clock = ManualClock()
    app = create_app(settings, clock=clock, routers=routers)
    with TestClient(app, base_url=base_url) as client:
        yield AuthEnv(settings=settings, clock=clock, client=client, app=app)
