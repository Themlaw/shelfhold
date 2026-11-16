"""The Komga key: sealed at rest by the SecretVault, held by the core, never given to a plugin.

The key is the API key of a dedicated administrator account of Komga (scan and `empty-trash` ask
for that role). It is sealed in the row of its target, like the Kavita key, under a name of its
own: the Kavita code, which reads `api_key`, can never take it for a Kavita key and run its calls
against a Komga.
"""

from __future__ import annotations

import json
from typing import Final

from sqlalchemy import Connection

from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.secrets import SecretVault

_TABLE: Final = "export_target"
_FIELD: Final = "komga_api_key"


class KomgaKey:
    """The API key of the dedicated Komga account."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("an empty Komga key")
        self._value = value
        register_secret_value(value)  # scrubbed by value from every later log line

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "KomgaKey(<redacted>)"

    __str__ = __repr__


def store_komga_key(
    vault: SecretVault, connection: Connection, target_id: str, key: KomgaKey
) -> None:
    payload = json.dumps({_FIELD: key.reveal()}).encode()
    vault.seal_row(connection, _TABLE, target_id, payload)


def load_komga_key(vault: SecretVault, connection: Connection, target_id: str) -> KomgaKey:
    document = json.loads(vault.open_row(connection, _TABLE, target_id))
    return KomgaKey(str(document[_FIELD]))
