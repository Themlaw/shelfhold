"""`secrets rotate`: new active key, rows re-sealed one by one, resumable (ENF-SEC-005).

The intention is durable before the first row moves: the keyring file carries the new active key
and a `rotation` marker (one atomic write). Each row then moves in its own transaction, under a
compare-and-swap on its old ciphertext, so a kill leaves every row either old or new, and both
open because retired keys are kept. Running the command again finds the marker and finishes the
job without creating a third key. The marker is cleared only when no row is left on an old key.

Only secrets are re-sealed. The tombstone table and journal are append-only and never rewritten
by a rotation (ADR-0048).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.errors import KeyRetirementRefusedError, SecretsError
from shelfhold.infrastructure.secrets.keyring import KeyringStore
from shelfhold.infrastructure.secrets.vault import SecretVault

RowCallback = Callable[[str, str], None]
MAX_PASSES = 3


class RotationIncompleteError(SecretsError):
    """Rows are still on an old key; run `secrets rotate` again (it resumes)."""

    def __init__(self, remaining: int) -> None:
        super().__init__(f"{remaining} secret(s) are still sealed under a retired key")
        self.remaining = remaining


@dataclass(frozen=True)
class RotationReport:
    new_kid: str
    resumed: bool
    reencrypted: int


def _begin_immediate(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN IMMEDIATE")  # take the write lock before reading the row


def _reseal_row(
    engine: Engine, vault: SecretVault, sealed: registry.SealedTable, row_id: str
) -> bool:
    active = vault.keyring.active.kid
    table = sealed.table
    with engine.connect() as connection:
        _begin_immediate(connection)
        try:
            row = (
                connection.execute(sa.select(table).where(sealed.pk == row_id))
                .mappings()
                .one_or_none()
            )
            if row is None or row["secret_kid"] in (None, active):
                connection.rollback()
                return False
            plaintext = vault.open_columns(connection, sealed.name, row_id, row)
            envelope = vault.seal(
                plaintext, table=sealed.name, column=registry.SEALED_COLUMN, row_id=row_id
            )
            result = connection.execute(
                sa.update(table)
                .where(
                    sealed.pk == row_id,
                    table.c.secret_kid == row["secret_kid"],
                    table.c.secret_nonce == row["secret_nonce"],
                )
                .values(**envelope.to_columns())
            )
            connection.commit()
            return result.rowcount == 1
        except BaseException:
            connection.rollback()
            raise


def rotate(
    store: KeyringStore,
    engine: Engine,
    *,
    now: datetime | None = None,
    on_row: RowCallback | None = None,
) -> RotationReport:
    """Rotate, or finish the rotation a previous run left behind. `on_row(table, id)` follows
    every committed row (progress display; tests use it to simulate a kill)."""
    with store.lock():
        keyring = store.load()
        resumed = keyring.rotation is not None
        if not resumed:
            keyring = keyring.rotated(now)
            store.write(keyring)  # the intention, durable before any row moves
        with engine.begin() as connection:
            registry.sync_registry(connection, keyring)
        vault = SecretVault(keyring)
        moved = 0
        for _ in range(MAX_PASSES):
            progressed = False
            for sealed in registry.sealed_tables().values():
                with engine.connect() as connection:
                    pending = registry.stale_row_ids(connection, sealed, keyring.active.kid)
                for row_id in pending:
                    if _reseal_row(engine, vault, sealed, row_id):
                        moved += 1
                        progressed = True
                        if on_row is not None:
                            on_row(sealed.name, row_id)
            if not progressed:
                break
        remaining = _remaining(engine, keyring.active.kid)
        if remaining:
            raise RotationIncompleteError(remaining)
        store.write(store.load().rotation_completed(now))
        return RotationReport(keyring.active.kid, resumed, moved)


def _remaining(engine: Engine, active_kid: str) -> int:
    with engine.connect() as connection:
        return sum(
            len(registry.stale_row_ids(connection, sealed, active_kid))
            for sealed in registry.sealed_tables().values()
        )


def retire_key(store: KeyringStore, engine: Engine, kid: str) -> None:
    """Drop the material of a retired key. Refused for the active key, a key a secret still
    uses, and any key a tombstone cites (the registry of issued kids, ADR-0048)."""
    with store.lock():
        keyring = store.load()
        key = keyring.find(kid)
        if key is None:
            raise KeyRetirementRefusedError(kid, "unknown")
        if key.state == "active":
            raise KeyRetirementRefusedError(kid, "active")
        with engine.connect() as connection:
            if kid in registry.cited_kids(connection):
                raise KeyRetirementRefusedError(kid, "cited_by_tombstone")
            if kid in registry.kids_in_use(connection):
                raise KeyRetirementRefusedError(kid, "in_use")
        store.write(keyring.without(kid))
