"""A bench for the webhooks of M4 L24: a migrated database, a vault, a scripted DNS and a recording
transport. Nothing leaves the process; the SSRF guard is the real one. Names and secrets are made
at run time."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx2
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import identity, platform
from shelfhold.infrastructure.secrets import SecretVault
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.registry import sync_registry
from shelfhold.integrations.webhooks import (
    WEBHOOK_EVENTS,
    Catalogue,
    Deliverer,
    Outbox,
    Subscription,
    SubscriptionStore,
    check_target,
)
from tests.fakes_clock import ManualClock
from tests.fakes_fetcher import FakeResolver, Recorder

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
PUBLIC = "93.184.216.34"
HOST = "hooks.example.org"
HOOK = f"https://{HOST}/in/one"


class Killed(BaseException):
    """What a `kill -9` looks like from inside: nothing after this point runs."""


@dataclass
class WebhookEnv:
    settings: Settings
    engine: Any
    vault: SecretVault
    clock: ManualClock
    resolver: FakeResolver
    recorder: Recorder
    store: SubscriptionStore
    catalogue: Catalogue
    admin_id: str
    users: dict[str, str] = field(default_factory=dict)

    def outbox(self) -> Outbox:
        return Outbox(self.engine, self.clock, catalogue=self.catalogue)

    def deliverer(self, engine: Any = None) -> Deliverer:
        return Deliverer(
            engine or self.engine, self.vault, self.clock, self.resolver,
            transport=self.recorder.transport, catalogue=self.catalogue,
        )  # fmt: skip

    def add_user(self, role: str, *, ceiling: int | None = None) -> str:
        user_id = _new_id()
        values: dict[str, Any] = {
            "id": user_id, "username": f"u-{user_id.lower()}", "role": role, "can_rate": False,
        }
        if role == "child":
            values["max_age_rating"] = 10 if ceiling is None else ceiling
        with write_transaction(self.engine) as connection:
            connection.execute(sa.insert(identity.user).values(**values))
        return user_id

    def subscribe(
        self,
        url: str = HOOK,
        types: tuple[str, ...] = ("unit.stored",),
        *,
        allow_private: bool = False,
        created_by: str | None = None,
    ) -> tuple[Subscription, str]:
        target = asyncio.run(check_target(url, allow_private=allow_private, resolver=self.resolver))
        with write_transaction(self.engine) as connection:
            return self.store.create(
                connection, created_by=created_by or self.admin_id, url=url,
                event_types=types, target=target, now=self.clock.now(),
            )  # fmt: skip

    def publish(
        self,
        event_type: str = "unit.stored",
        payload: dict[str, Any] | None = None,
        *,
        visibility: str = "ops",
        schema_version: int = 1,
        actor_id: str | None = None,
    ) -> int:
        with write_transaction(self.engine) as connection:
            result = connection.execute(
                sa.insert(platform.event).values(
                    type=event_type, schema_version=schema_version,
                    payload=payload if payload is not None else {"count": 1},
                    occurred_at=self.clock.now(), actor_id=actor_id, visibility=visibility,
                )
            )
            return int(result.inserted_primary_key[0])

    def rows(self) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            found = connection.execute(
                sa.text("SELECT * FROM webhook_outbox ORDER BY event_id, schema_version")
            )
            return [dict(r._mapping) for r in found]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.recorder.requests]


def _new_id() -> str:
    from shelfhold.infrastructure.ids import new_ulid

    return new_ulid()


@contextmanager
def webhook_env(
    settings: Settings,
    *,
    dns: dict[str, Any] | None = None,
    respond: Callable[[httpx2.Request], httpx2.Response] | None = None,
    catalogue: Catalogue | None = None,
) -> Iterator[WebhookEnv]:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        vault = SecretVault(Keyring.generate(NOW))
        with write_transaction(engine) as connection:
            sync_registry(connection, vault.keyring)
        table: dict[str, Any] = {HOST: [PUBLIC], "inside.example.org": ["10.0.0.5"]}
        table.update(dns or {})
        env = WebhookEnv(
            settings, engine, vault, ManualClock(NOW), FakeResolver(table), Recorder(respond),
            SubscriptionStore(vault), catalogue or WEBHOOK_EVENTS, "",
        )  # fmt: skip
        env.admin_id = env.add_user("admin")
        yield env
    finally:
        engine.dispose()
