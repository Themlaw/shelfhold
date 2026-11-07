"""The keys and sealed rows in the database (`secret_key`, `tombstone_kid`, sealed columns).

The key material lives only in the keyring file; `secret_key` mirrors kid, state and dates so
that sealed rows can reference their key and so that a restore can be checked against it.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
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
                set_={  # `compromised` is one-way: a keyring copy cannot lift it
                    "state": sa.case(
                        (_secret_key.c.state == "compromised", _secret_key.c.state),
                        else_=statement.excluded.state,
                    ),
                    "retired_at": sa.case(
                        (_secret_key.c.state == "compromised", _secret_key.c.retired_at),
                        else_=statement.excluded.retired_at,
                    ),
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


def _read_only(database_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)


def _first_row(connection: sqlite3.Connection, sql: str, *params: str) -> object:
    """The first row of `sql`, or None when the table is not there yet (not migrated).

    Any other database error (locked, unreadable, corrupt) is raised: the caller must not read
    "I could not look" as "there is nothing".
    """
    try:
        return connection.execute(sql, params).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise


def registry_has_keys(database_path: Path) -> bool:
    """True if the database already knows a key or holds a sealed secret (read only).

    Raises `sqlite3.Error` if it cannot tell.
    """
    if not database_path.exists():
        return False
    connection = _read_only(database_path)
    try:
        if _first_row(connection, "SELECT 1 FROM secret_key LIMIT 1"):
            return True
        return any(
            _first_row(
                connection,
                f'SELECT 1 FROM "{name}" WHERE secret_kid IS NOT NULL LIMIT 1',  # noqa: S608
            )
            for name in sealed_tables()
        )
    finally:
        connection.close()


def rows_not_on_key(database_path: Path, active_kid: str) -> int:
    """How many sealed rows are not under `active_kid` (a stale process, a half rotation)."""
    if not database_path.exists():
        return 0
    connection = _read_only(database_path)
    try:
        total = 0
        for name in sealed_tables():
            row = _first_row(
                connection,
                f'SELECT count(*) FROM "{name}" WHERE secret_kid IS NOT NULL '  # noqa: S608
                "AND secret_kid <> ?",
                active_kid,
            )
            total += int(row[0]) if isinstance(row, tuple) else 0
        return total
    finally:
        connection.close()


@dataclass(frozen=True)
class TombstoneKids:
    first_at: datetime | None  # when the first tombstone was written (its kid registered)
    kids: frozenset[str]
    lost: frozenset[str]  # covered by the "lost keyring" waiver


def tombstone_kids(database_path: Path) -> TombstoneKids:
    """The registry of issued kids, read only (`status`, start-up check)."""
    empty = TombstoneKids(None, frozenset(), frozenset())
    if not database_path.exists():
        return empty
    connection = _read_only(database_path)
    try:
        try:
            rows = connection.execute("SELECT kid, created_at FROM tombstone_kid").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return empty
            raise
        try:
            lost = {r[0] for r in connection.execute("SELECT kid FROM tombstone_lost_kid")}
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            lost = set()
    finally:
        connection.close()
    stamps = [datetime.fromisoformat(str(r[1])).replace(tzinfo=UTC) for r in rows]
    return TombstoneKids(min(stamps, default=None), frozenset(r[0] for r in rows), frozenset(lost))
