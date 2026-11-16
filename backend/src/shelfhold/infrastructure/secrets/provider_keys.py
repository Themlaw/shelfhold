"""The keys of the metadata providers that are the instance's own, sealed at rest (EF-BIB-013).

Hardcover's token and Google Books' key are given once by the operator (`meta provider-key set`),
sealed by the `SecretVault` in `provider_secret`, and read only by the core when it asks the
provider. They are never given back by a surface, never put in a log, an audit row, the diagnostic
bundle or the full open export; the browser has no road to them, since no route reads them.
"""

from __future__ import annotations

import json
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.schema import acquisition
from shelfhold.infrastructure.secrets import SecretVault

__all__ = [
    "PROVIDERS",
    "ProviderKey",
    "clear_provider_key",
    "load_provider_key",
    "provider_key_present",
    "store_provider_key",
]

PROVIDERS: Final = ("hardcover", "googlebooks")
MAX_LENGTH: Final = 2000
_TABLE: Final = "provider_secret"
_FIELD: Final = "provider_key"
_table = acquisition.provider_secret


class ProviderKey:
    """A provider's key: no repr, no str and no format gives it away."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value or not value.isprintable() or any(c.isspace() for c in value):
            raise ValueError("a provider key is one printable word")
        if len(value) > MAX_LENGTH:
            raise ValueError("a provider key is not that long")
        self._value = value
        register_secret_value(value)  # scrubbed by value from every later log line

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "ProviderKey(<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)


def _known(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"no key is kept for {provider!r}")
    return provider


def store_provider_key(
    vault: SecretVault, connection: Connection, provider: str, key: ProviderKey
) -> None:
    """Seal `key` for `provider`, replacing the one it had."""
    _known(provider)
    connection.execute(sqlite_insert(_table).values(provider=provider).on_conflict_do_nothing())
    payload = json.dumps({_FIELD: key.reveal()}).encode()
    vault.seal_row(connection, _TABLE, provider, payload)
    connection.execute(
        sa.update(_table)
        .where(_table.c.provider == provider)
        .values(updated_at=sa.func.current_timestamp())
    )


def load_provider_key(
    vault: SecretVault, connection: Connection, provider: str
) -> ProviderKey | None:
    _known(provider)
    if not provider_key_present(connection, provider):
        return None
    document = json.loads(vault.open_row(connection, _TABLE, provider))
    return ProviderKey(str(document[_FIELD]))


def provider_key_present(connection: Connection, provider: str) -> bool:
    return (
        connection.execute(
            sa.select(_table.c.provider).where(
                _table.c.provider == provider, _table.c.secret_ct.is_not(None)
            )
        ).first()
        is not None
    )


def clear_provider_key(connection: Connection, provider: str) -> bool:
    """Erase the key (the row goes with it); `False` when there was none."""
    _known(provider)
    return connection.execute(sa.delete(_table).where(_table.c.provider == provider)).rowcount > 0
