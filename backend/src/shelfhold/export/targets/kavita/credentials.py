"""The admin key: sealed at rest by the SecretVault, held by the core, never given to a plugin."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import Connection

from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.secrets import SecretVault

_TABLE: Final = "export_target"
_SECRET_WORDS: Final = frozenset(
    {"key", "apikey", "secret", "token", "password", "passwd", "auth", "authorization", "cookie",
     "credential", "credentials", "session"}
)  # fmt: skip
_WORD: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


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


def _looks_secret(name: str) -> bool:
    """Whole-word match on the name (`api_key`, `apiKey`, `X-Token`), so `author` is kept."""
    words = {w.lower() for w in _WORD.findall(name)}
    return bool(words & _SECRET_WORDS)


def _clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _clean(v) for k, v in value.items() if not _looks_secret(str(k))}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def public_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """The configuration a destination plugin may see: no field that looks like a credential.

    Filtered at every depth. The key is not in `export_target.config` to begin with; this is the
    second lock.
    """
    cleaned: dict[str, Any] = _clean(config)
    return cleaned
