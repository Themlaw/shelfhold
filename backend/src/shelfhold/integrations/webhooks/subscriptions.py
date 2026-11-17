"""Registering a webhook: the address through the SSRF guard, the secret sealed.

`check_target` is the one verdict on an address, at registration and, with the same guard, at every
delivery (the Fetcher resolves again each time). A public host is reachable over https. A private
host is reachable only when the administrator says so, as exactly `host:port` with the addresses it
had then (`LanTargets`: a record that moves is refused, and the cloud metadata service is never
allowed). The address and the signing secret are one sealed document bound to the row; only the host
is ever shown, because the path of a webhook address is often a token.
"""

from __future__ import annotations

import ipaddress
import json
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.events.integration_feed import last_event_id
from shelfhold.infrastructure.db import read_transaction
from shelfhold.infrastructure.fetcher import (
    GuardedFetcher,
    InvalidLanTarget,
    LanTargets,
    Resolver,
    block_reason,
    parse_host,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.schema import webhooks as schema
from shelfhold.infrastructure.secrets import SecretVault
from shelfhold.integrations.webhooks.catalogue import WEBHOOK_EVENTS, Catalogue

__all__ = [
    "MAX_EVENT_TYPES",
    "MAX_URL_LENGTH",
    "Opened",
    "Subscription",
    "SubscriptionStore",
    "Target",
    "WebhookRefused",
    "check_target",
]

MAX_URL_LENGTH: Final = 2000
MAX_EVENT_TYPES: Final = 64
_TABLE: Final = "webhook_subscription"
_table = schema.webhook_subscription


class WebhookRefused(ValueError):
    """A registration that is not accepted. `reason` is a closed code, never the address."""

    code = "webhook.refused"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    private: bool  # the administrator allowed a private address for exactly this host and port
    addresses: tuple[str, ...]  # what it resolved to when it was checked

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


async def check_target(url: str, *, allow_private: bool, resolver: Resolver) -> Target:
    """The verdict on a webhook address. Raises `WebhookRefused` for what is wrong with the
    address itself, `SsrfBlocked` (or another `FetchError`) for where it leads."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH or not url.isascii():
        raise WebhookRefused("invalid_url")
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise WebhookRefused("invalid_url")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise WebhookRefused("invalid_url") from None
    if parts.scheme not in ("http", "https"):
        raise WebhookRefused("scheme_not_allowed")
    if not host or "@" in parts.netloc or parts.fragment:
        raise WebhookRefused("invalid_url")
    port = port or (443 if parts.scheme == "https" else 80)
    parse_host(host)  # a non-canonical notation is refused here (SsrfBlocked)
    entry = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    private = False
    if allow_private:
        try:
            lan = await LanTargets.declare([entry], resolver)
        except InvalidLanTarget:
            raise WebhookRefused("private_target_refused") from None
        declared = lan.addresses(host, port) or frozenset()
        private = any(block_reason(a) is not None for a in declared)
        if private:
            addresses = sorted(declared, key=str)
    if not private:
        fetcher = GuardedFetcher(resolver)
        try:
            addresses = await fetcher.check_destination(host, port, allow_lan=False)
        finally:
            await fetcher.aclose()
        if parts.scheme != "https":
            raise WebhookRefused("https_required")
    return Target(parts.scheme, host, port, private, tuple(str(a) for a in addresses))


@dataclass(frozen=True)
class Subscription:
    """What may be shown of a subscription: never the address, never the secret."""

    id: str
    created_by: str | None
    url_host: str
    event_types: tuple[str, ...]
    private_target: str | None
    private_addresses: tuple[str, ...]
    enabled: bool
    created_at: datetime
    last_event_id: int


@dataclass(frozen=True)
class Opened:
    """A subscription's address and secret, opened to deliver: neither is ever in a repr."""

    subscription: Subscription
    url: str = field(repr=False)
    secret: str = field(repr=False)


def _register_url(url: str) -> None:
    """Scrub the address from every later log line, and its path and query on their own: the
    Fetcher names a failed call by host and path, and the path of a webhook is often a token."""
    register_secret_value(url)
    parts = urlsplit(url)
    for piece in (parts.path, parts.query):
        register_secret_value(piece.strip("/"))


def _view(row: Any) -> Subscription:
    return Subscription(
        id=row.id, created_by=row.created_by, url_host=row.url_host,
        event_types=tuple(row.event_types), private_target=row.private_target,
        private_addresses=tuple(row.private_addresses or ()), enabled=bool(row.enabled),
        created_at=row.created_at, last_event_id=int(row.last_event_id),
    )  # fmt: skip


class SubscriptionStore:
    def __init__(self, vault: SecretVault, *, catalogue: Catalogue = WEBHOOK_EVENTS) -> None:
        self._vault = vault
        self._catalogue = catalogue

    def create(
        self,
        connection: Connection,
        *,
        created_by: str,
        url: str,
        event_types: Iterable[str],
        target: Target,
        now: datetime,
    ) -> tuple[Subscription, str]:
        """Seal the address and a new secret; the secret is returned here and nowhere again."""
        types = tuple(dict.fromkeys(event_types))
        if not types or len(types) > MAX_EVENT_TYPES:
            raise WebhookRefused("event_types")
        if not set(types) <= self._catalogue.types():
            raise WebhookRefused("event_types")
        secret = "whsec_" + secrets.token_urlsafe(32)
        register_secret_value(secret)
        _register_url(url)
        row_id = new_ulid()
        connection.execute(
            sa.insert(_table).values(
                id=row_id,
                created_by=created_by,
                url_host=target.host,
                event_types=list(types),
                private_target=target.authority if target.private else None,
                private_addresses=list(target.addresses) if target.private else None,
                last_event_id=last_event_id(connection),
                created_at=now,
            )
        )
        document = json.dumps({"url": url, "secret": secret}).encode()
        self._vault.seal_row(connection, _TABLE, row_id, document)
        return self.get(connection, row_id) or _gone(), secret

    def get(self, connection: Connection, subscription_id: str) -> Subscription | None:
        row = connection.execute(sa.select(_table).where(_table.c.id == subscription_id)).first()
        return None if row is None else _view(row)

    def listing(self, connection: Connection) -> list[Subscription]:
        rows = connection.execute(sa.select(_table).order_by(_table.c.created_at, _table.c.id))
        return [_view(r) for r in rows]

    def delete(self, connection: Connection, subscription_id: str) -> bool:
        deleted = connection.execute(sa.delete(_table).where(_table.c.id == subscription_id))
        return deleted.rowcount > 0

    def open(self, engine: Engine | Connection, subscription_id: str) -> Opened:
        """The address and the secret, for a delivery (the envelope is bound to the row)."""
        if isinstance(engine, Engine):
            with read_transaction(engine) as connection:
                return self.open(connection, subscription_id)
        row = engine.execute(sa.select(_table).where(_table.c.id == subscription_id)).one()
        document = json.loads(self._vault.open_row(engine, _TABLE, subscription_id))
        register_secret_value(document["secret"])
        _register_url(document["url"])
        return Opened(_view(row), document["url"], document["secret"])


def _gone() -> Subscription:  # pragma: no cover - the row was just written
    raise LookupError("the subscription is gone")
