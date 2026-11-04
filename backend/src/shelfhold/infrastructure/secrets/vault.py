"""Seal and open secrets with the keyring; the one place that knows both keys and rows."""

import json
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.envelope import (
    Envelope,
    check_supported,
    open_with,
    seal,
)
from shelfhold.infrastructure.secrets.errors import EnvelopeFormatError, UnknownKidError
from shelfhold.infrastructure.secrets.keyring import Keyring


def _remember(plaintext: bytes) -> None:
    """Scrub this secret by value from every later log line, exceptions included.

    The whole text is registered, and when it is a JSON document every string in it, since the
    value that leaks is usually one field (a password), not the document.
    """
    text = plaintext.decode("utf-8", errors="replace")
    register_secret_value(text)
    try:
        parsed = json.loads(text)
    except ValueError:
        return
    pending = [parsed]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            register_secret_value(item)
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


class SecretVault:
    def __init__(self, keyring: Keyring) -> None:
        self.keyring = keyring

    def __repr__(self) -> str:
        return f"SecretVault(active={self.keyring.active.kid!r})"

    def seal(self, plaintext: bytes, *, table: str, column: str, row_id: str) -> Envelope:
        _remember(plaintext)
        key = self.keyring.active
        return seal(key.material, key.kid, plaintext, table=table, column=column, row_id=row_id)

    def open(self, envelope: Envelope, *, table: str, column: str, row_id: str) -> bytes:
        check_supported(envelope)
        key = self.keyring.find(envelope.kid)
        if key is None:
            raise UnknownKidError(envelope.kid)
        plaintext = open_with(key.material, envelope, table=table, column=column, row_id=row_id)
        _remember(plaintext)
        return plaintext

    # --- rows with sealed columns -------------------------------------------------------------

    def seal_row(self, connection: Connection, table: str, row_id: str, plaintext: bytes) -> None:
        """Seal `plaintext` into the secret columns of an existing row, bound to that row."""
        sealed = registry.sealed_table(table)
        registry.register_active_key(connection, self.keyring)
        envelope = self.seal(plaintext, table=table, column=registry.SEALED_COLUMN, row_id=row_id)
        result = connection.execute(
            sa.update(sealed.table).where(sealed.pk == row_id).values(**envelope.to_columns())
        )
        if result.rowcount != 1:
            raise LookupError(f"no row {row_id!r} in {table}")

    def open_row(self, connection: Connection, table: str, row_id: str) -> bytes:
        sealed = registry.sealed_table(table)
        row = (
            connection.execute(sa.select(sealed.table).where(sealed.pk == row_id)).mappings().one()
        )
        if row["secret_kid"] is None:
            raise LookupError(f"row {row_id!r} of {table} holds no secret")
        return self.open_columns(connection, table, row_id, row)

    def open_columns(
        self, connection: Connection, table: str, row_id: str, row: Mapping[Any, Any]
    ) -> bytes:
        alg = registry.key_alg(connection, row["secret_kid"])
        if alg is None:
            raise EnvelopeFormatError("the key of this row is not registered")
        envelope = Envelope.from_columns(row, alg=alg)
        return self.open(envelope, table=table, column=registry.SEALED_COLUMN, row_id=row_id)
