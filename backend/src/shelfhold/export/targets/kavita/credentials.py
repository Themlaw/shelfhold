"""The admin key: sealed at rest by the SecretVault, held by the core, never given to a plugin."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import Connection

from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.secrets import SecretVault

_TABLE: Final = "export_target"
_SECRET_HINTS: Final = (
    "key",
    "secret",
    "token",
    "password",
    "passwd",
    "auth",
    "cookie",
    "credential",
)


class AdminKey:
    """The named, expiring key of the dedicated (administrator) Kavita account."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("an empty Kavita key")
        self._value = value
        register_secret_value(value)  # scrubbed by value from every later log line

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "AdminKey(<redacted>)"

    __str__ = __repr__


def store_admin_key(
    vault: SecretVault, connection: Connection, target_id: str, key: AdminKey
) -> None:
    payload = json.dumps({"api_key": key.reveal()}).encode()
    vault.seal_row(connection, _TABLE, target_id, payload)


def load_admin_key(vault: SecretVault, connection: Connection, target_id: str) -> AdminKey:
    document = json.loads(vault.open_row(connection, _TABLE, target_id))
    return AdminKey(str(document["api_key"]))


def public_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """The configuration a destination plugin may see: no field that looks like a credential.

    The key is not in `export_target.config` to begin with; this is the second lock.
    """
    return {
        name: value
        for name, value in config.items()
        if not any(hint in name.lower() for hint in _SECRET_HINTS)
    }
