"""The Apprise destination of one adult profile, sealed, and the way a notice reaches it (M4 L39,
EF-INT-021, ENF-SEC-005, ENF-SEC-016).

The shared channels of the instance are read by everyone, a child included, so nothing that names a
work leaves through them. An adult (admin or member) may seal a destination of their own: the
titles of their notices go there, **and only there**. The URL is checked like every channel URL
(allowlist of schemes, no redirect, the SSRF guard), sealed by the `SecretVault` to its row, and
never read back by the API: only the sender opens it, at each send.

A child has none: `set` refuses it whoever asks, and `ProfileDelivery` does not even look for one
for a child (a row written by hand or restored is never used). One profile, one destination; nobody
sets another's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fetcher import LanTargets, Resolver, SystemResolver
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity
from shelfhold.infrastructure.secrets import SecretVault, registry
from shelfhold.integrations.apprise.destination import (
    ChannelUrlRefused,
    register_url_secrets,
    validate_channel_url,
)
from shelfhold.integrations.apprise.render import Message
from shelfhold.integrations.apprise.send import SendResult, send_with_apprise

__all__ = ["ProfileDelivery", "ProfileDestinationError", "ProfileDestinations"]

log = structlog.get_logger(__name__)

TABLE: Final = "notify_destination"
ADULTS: Final = frozenset({"admin", "member"})
_row = identity.notify_destination
_user = identity.user


class ProfileDestinationError(Exception):
    """A refusal with a stable code: `not_found`, `child_has_no_destination`, `invalid_url`. The
    message is the code, never the URL."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _role(connection: sa.Connection, user_id: str) -> str | None:
    found = connection.execute(sa.select(_user.c.role).where(_user.c.id == user_id)).scalar()
    return None if found is None else str(found)


class ProfileDestinations:
    def __init__(
        self,
        engine: Engine,
        vault_of: Callable[[], SecretVault],
        clock: Clock,
        *,
        lan_targets: Callable[[], LanTargets] = LanTargets.empty,
        resolver: Resolver | None = None,
    ) -> None:
        """`vault_of` opens the vault when a URL is sealed or opened, not before."""
        self._engine = engine
        self._vault_of = vault_of
        self._clock = clock
        self._lan_targets = lan_targets
        self._resolver = resolver or SystemResolver()

    def __repr__(self) -> str:
        return "ProfileDestinations()"

    def configured(self, user_id: str) -> bool:
        with self._engine.connect() as connection:
            if _role(connection, user_id) not in ADULTS:
                return False
            return (
                connection.execute(sa.select(_row.c.id).where(_row.c.user_id == user_id)).first()
                is not None
            )

    def set(self, user_id: str, url: str) -> None:
        """Seal `url` as the destination of this adult profile (replacing the previous one)."""
        with self._engine.connect() as connection:
            role = _role(connection, user_id)
        if role is None:
            raise ProfileDestinationError("not_found")
        if role not in ADULTS:
            raise ProfileDestinationError("child_has_no_destination")
        try:
            validated = asyncio.run(
                validate_channel_url(url, lan=self._lan_targets(), resolver=self._resolver)
            )
        except ChannelUrlRefused:
            raise ProfileDestinationError("invalid_url") from None
        vault = self._vault_of()
        with write_transaction(self._engine) as connection:
            held = connection.execute(
                sa.select(_row.c.id).where(_row.c.user_id == user_id)
            ).scalar()
            row_id = str(held) if held is not None else new_ulid()
            registry.register_active_key(connection, vault.keyring)
            envelope = vault.seal(
                validated.url.encode(), table=TABLE, column=registry.SEALED_COLUMN, row_id=row_id
            )
            if held is None:
                connection.execute(
                    sa.insert(_row).values(id=row_id, user_id=user_id, **envelope.to_columns())
                )
            else:
                connection.execute(
                    sa.update(_row)
                    .where(_row.c.id == row_id)
                    .values(updated_at=self._clock.now(), **envelope.to_columns())
                )

    def clear(self, user_id: str) -> bool:
        with write_transaction(self._engine) as connection:
            gone = connection.execute(sa.delete(_row).where(_row.c.user_id == user_id))
        return bool(gone.rowcount)


class ProfileDelivery:
    """The way out for a profile that has a destination: `deliver(user_id, message)` is `None`
    when this person has none (the sender then uses the shared channels, without a title), else
    whether it was sent. Never opens a destination for a child."""

    def __init__(
        self,
        engine: Engine,
        vault_of: Callable[[], SecretVault],
        *,
        send: Callable[[str, Message], SendResult] = send_with_apprise,
        lan_targets: Callable[[], LanTargets] = LanTargets.empty,
        resolver: Resolver | None = None,
    ) -> None:
        self._engine = engine
        self._vault_of = vault_of
        self._send = send
        self._lan_targets = lan_targets
        self._resolver = resolver or SystemResolver()

    def has_destination(self, user_id: str) -> bool:
        with self._engine.connect() as connection:
            if _role(connection, user_id) not in ADULTS:
                return False
            return (
                connection.execute(sa.select(_row.c.id).where(_row.c.user_id == user_id)).first()
                is not None
            )

    def __call__(self, user_id: str, message: Message) -> bool | None:
        try:
            with self._engine.connect() as connection:
                if _role(connection, user_id) not in ADULTS:
                    return None
                held = (
                    connection.execute(sa.select(_row).where(_row.c.user_id == user_id))
                    .mappings()
                    .first()
                )
                if held is None:
                    return None
                url = self._vault_of().open_columns(connection, TABLE, held["id"], held).decode()
        except Exception:  # undecryptable: the row stays, the sweep tries again, never shared
            log.warning("notify.destination_unreadable")
            return False
        register_url_secrets(url)
        try:
            asyncio.run(validate_channel_url(url, lan=self._lan_targets(), resolver=self._resolver))
            return bool(self._send(url, message).ok)
        except Exception:
            log.warning("notify.destination_send_failed")  # never the URL or the text
            return False
