"""EF-SRC-023, EF-SRC-008: Basic credentials from `source_account` (SecretVault), injected by the
real IntegrationFetcher stack, against the simulated Suwayomi in `basic_auth` mode."""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from shelfhold.plugin_host.authflow import AuthInjectingFetcher, load_account_secret
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher.client import IntegrationFetcher
from shelfhold.infrastructure.fetcher.lan import LanTargets
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import identity, metadata
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.sdk.models import AuthBasic
from shelfhold.sdk.source import AuthRequired
from tests.fakes.suwayomi import BASE_URL, PASSWORD, USER, FakeSuwayomi
from tests.fakes_fetcher import FakeResolver

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
source_account = metadata.tables["source_account"]
LAN_IP = "192.168.1.50"
ARGON = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"  # synthetic hash, never verified


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    yield engine
    engine.dispose()


def _account(engine: Engine, vault: SecretVault, secret: dict[str, str]) -> str:
    account_id = new_ulid()
    user_id = new_ulid()
    with engine.begin() as conn:
        registry.sync_registry(conn, vault.keyring)
        conn.execute(
            sa.insert(identity.user).values(
                id=user_id,
                username=f"u-{user_id}",
                password_hash=ARGON,
                role="admin",
                can_rate=True,
            )
        )
        conn.execute(
            sa.insert(source_account).values(
                id=account_id, user_id=user_id, plugin_id="suwayomi", kind="admin",
                auth_mode="basic_auth", auth_kind="basic",
            )
        )  # fmt: skip
        vault.seal_row(conn, "source_account", account_id, json.dumps(secret).encode())
    return account_id


def _stack(server: FakeSuwayomi) -> IntegrationFetcher:
    dns = FakeResolver({"suwayomi.lan": [LAN_IP]})
    lan = asyncio.run(LanTargets.declare(["suwayomi.lan:4567"], dns))
    return IntegrationFetcher(dns, lan_targets=lan, transport=server.transport)


@pytest.mark.req("EF-SRC-023", "EF-SRC-008")
def test_suwayomi_basic_auth_m1(engine: Engine) -> None:
    vault = SecretVault(Keyring.generate(NOW))
    account = _account(engine, vault, {"username": USER, "password": PASSWORD})
    server = FakeSuwayomi(auth_mode="basic_auth")
    service = SuwayomiService(BASE_URL)

    with engine.connect() as conn:
        secret = load_account_secret(conn, vault, account)
    assert secret == {"username": USER, "password": PASSWORD}
    with engine.connect() as conn:  # the column holds ciphertext, never the password
        raw: Any = conn.execute(sa.select(source_account.c.secret_ct)).scalar_one()
    assert PASSWORD.encode() not in bytes(raw)

    authed: Any = AuthInjectingFetcher(_stack(server), AuthBasic(), secret)
    health = asyncio.run(service.semantic_health(authed))
    assert health.ok  # the request succeeded with the secret injected by the Fetcher
    assert asyncio.run(service.auth_mode(authed)) == "basic_auth"
    assert {r.headers["authorization"].split()[0] for r in server.requests} == {"Basic"}
    assert all(r.headers.get("host", "").startswith("suwayomi.lan") for r in server.requests)

    # without the secret the same call fails as an authentication problem, nothing else
    server.requests.clear()
    with pytest.raises(AuthRequired):
        asyncio.run(service.semantic_health(_stack(server)))  # type: ignore[arg-type]
    wrong: Any = AuthInjectingFetcher(
        _stack(server), AuthBasic(), {"username": USER, "password": "changed"}
    )
    with pytest.raises(AuthRequired):
        asyncio.run(service.semantic_health(wrong))
