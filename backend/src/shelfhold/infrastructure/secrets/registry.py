"""The keys and sealed rows in the database (`secret_key`, `tombstone_kid`, sealed columns).

The key material lives only in the keyring file; `secret_key` mirrors kid, state and dates so
that sealed rows can reference their key and so that a restore can be checked against it.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import Insert as SqliteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.schema import metadata
from shelfhold.infrastructure.secrets.keyring import Key, Keyring

SEALED_COLUMN = "secret_ct"  # the `column` coordinate of the associated data of sealed rows
_SEALED_COLUMNS = ("secret_kid", "secret_v", "secret_nonce", "secret_ct")


@dataclass(frozen=True)
class SealedTable:
    name: str
    table: sa.Table
    pk: sa.Column[str]


def sealed_tables() -> dict[str, SealedTable]:
    """Every table that carries the four sealed columns, found in the schema itself.

    A new table with `sealed_secret_columns()` is rotated without anyone listing it here.
    """
    found: dict[str, SealedTable] = {}
    for name, table in sorted(metadata.tables.items()):
        if all(column in table.c for column in _SEALED_COLUMNS):
            pk = list(table.primary_key.columns)
            if len(pk) != 1:
                raise RuntimeError(f"sealed table {name} must have a single-column key")
            found[name] = SealedTable(name, table, pk[0])
    return found


def sealed_table(name: str) -> SealedTable:
    try:
        return sealed_tables()[name]
    except KeyError:
        raise ValueError(f"{name!r} has no sealed secret columns") from None


_secret_key = metadata.tables["secret_key"]
_tombstone_kid = metadata.tables["tombstone_kid"]


def _insert(key: Key) -> SqliteInsert:
    return sqlite_insert(_secret_key).values(
        kid=key.kid,
        state=key.state,
        alg=key.alg,
        created_at=key.created_at,
        retired_at=key.retired_at,
    )


def register_active_key(connection: Connection, keyring: Keyring) -> None:
    connection.execute(_insert(keyring.active).on_conflict_do_nothing(index_elements=["kid"]))


def sync_registry(connection: Connection, keyring: Keyring) -> None:
    """Make `secret_key` say what the keyring says (idempotent; never deletes)."""
    for key in keyring.keys:
        statement = _insert(key)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["kid"],
                set_={
                    "state": statement.excluded.state,
                    "retired_at": statement.excluded.retired_at,
                },
            )
        )


def key_alg(connection: Connection, kid: str) -> str | None:
    return connection.execute(
        sa.select(_secret_key.c.alg).where(_secret_key.c.kid == kid)
    ).scalar_one_or_none()


def cited_kids(connection: Connection) -> set[str]:
    """Every kid a tombstone ever used: never retired (ADR-0048, registry of issued kids)."""
    return set(connection.execute(sa.select(_tombstone_kid.c.kid)).scalars())


def kids_in_use(connection: Connection) -> set[str]:
    used: set[str] = set()
    for sealed in sealed_tables().values():
        column = sealed.table.c.secret_kid
        used |= set(
            connection.execute(sa.select(column).where(column.is_not(None)).distinct()).scalars()
        )
    return used


def stale_row_ids(connection: Connection, sealed: SealedTable, active_kid: str) -> list[str]:
    column = sealed.table.c.secret_kid
    statement = sa.select(sealed.pk).where(column.is_not(None), column != active_kid)
    return [str(value) for value in connection.execute(statement.order_by(sealed.pk)).scalars()]


def registry_has_keys(database_path: Path) -> bool:
    """True if the database already knows a key or holds a sealed secret (read only)."""
    if not database_path.exists():
        return False
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if connection.execute("SELECT 1 FROM secret_key LIMIT 1").fetchone():
            return True
        for name in sealed_tables():
            if connection.execute(
                f'SELECT 1 FROM "{name}" WHERE secret_kid IS NOT NULL LIMIT 1'  # noqa: S608
            ).fetchone():
                return True
        return False
    except sqlite3.OperationalError:
        return False  # not migrated yet
    finally:
        connection.close()
