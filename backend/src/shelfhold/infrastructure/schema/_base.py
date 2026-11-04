"""Shared building blocks of the SQLAlchemy Core schema: one MetaData, UTC datetimes, checks.

Decisions (PLAN-M1a A17): the schema is plain SQLAlchemy Core, grouped by area in sibling
modules, all registered on the single `metadata` below. Contexts never import each other's
tables; the domain never imports this package.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Dialect

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


class UtcDateTime(sa.TypeDecorator[datetime]):
    """Instants are stored in UTC and come back as timezone-aware UTC (ENF-TIME-001).

    A naive datetime is a bug (which zone?) and is refused rather than guessed. Local time is
    a rendering concern of the interface, never of storage.
    """

    impl = sa.DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime: pass a timezone-aware value, it is stored in UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def now_column(name: str = "created_at") -> sa.Column[datetime]:
    """NOT NULL instant defaulting to the database's UTC clock."""
    return sa.Column(
        name, UtcDateTime(), nullable=False, server_default=sa.func.current_timestamp()
    )


def _quoted(column: str) -> str:
    return f'"{column}"'


def ulid_check(column: str) -> sa.CheckConstraint:
    """The column holds a 26-character Crockford base32 ULID (ENF-OPS-020)."""
    c = _quoted(column)
    return sa.CheckConstraint(
        f"length({c}) = 26 AND {c} NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'", name=f"{column}_ulid"
    )


def ulid_column(name: str, *, primary_key: bool = False, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(
        name, sa.String(26), primary_key=primary_key, nullable=nullable and not primary_key
    )


def prefixed_hash_check(
    column: str, algorithm: str = "sha256", digest_length: int = 64
) -> sa.CheckConstraint:
    """`<algorithm>:<lowercase hex>`: a bare digest never says how it was computed."""
    c = _quoted(column)
    prefix = f"{algorithm}:"
    return sa.CheckConstraint(
        f"length({c}) = {len(prefix) + digest_length} AND {c} GLOB '{prefix}*' "
        f"AND substr({c}, {len(prefix) + 1}) NOT GLOB '*[^0-9a-f]*'",
        name=f"{column}_{algorithm}_prefixed",
    )


def in_check(column: str, values: Iterable[str], *, name: str | None = None) -> sa.CheckConstraint:
    listing = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{_quoted(column)} IN ({listing})", name=name or f"{column}_values")


def positive_check(column: str, *, minimum: int = 0) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{_quoted(column)} >= {minimum}", name=f"{column}_min")


def bool_column(name: str, *, default: bool, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name, sa.Boolean(), nullable=nullable, server_default=sa.true() if default else sa.false()
    )


def add_trigger(table: sa.Table, name: str, sql: str) -> None:
    """Create a trigger with the table, in `create_all` as in the (frozen) migration.

    The migration carries its own copy of the statement; the DDL parity test keeps both equal.
    """
    statement = f"CREATE TRIGGER {name} {sql}"
    sa.event.listen(table, "after_create", sa.DDL(statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]


def json_shape_check(column: str, shape: str) -> sa.CheckConstraint:
    """The JSON document is an `array` or an `object` (a bare scalar is a bug, not data).

    SQLite allows no sub-query in a CHECK: the shape of the elements is the application's job.
    """
    c = _quoted(column)
    label = "list" if shape == "array" else "object"
    return sa.CheckConstraint(
        f"json_valid({c}) AND json_type({c}) = '{shape}'", name=f"{column}_{label}"
    )


def relative_path_check(column: str) -> sa.CheckConstraint:
    """A path relative to its root: absolute paths and `..` segments never get in."""
    c = _quoted(column)
    return sa.CheckConstraint(
        f"{c} NOT GLOB '/*' AND {c} <> '..' AND {c} NOT GLOB '../*' "
        f"AND {c} NOT GLOB '*/../*' AND {c} NOT GLOB '*/..'",
        name=f"{column}_relative",
    )


def add_view(table: sa.Table, name: str, select: str) -> None:
    """Create a view once `table` exists, in `create_all` as in the (frozen) migration.

    SQLite checks nothing about the tables a view reads until it is queried, so the view hangs on
    one of them. It goes before them when the metadata is dropped.
    """
    statement = f"CREATE VIEW {name} AS {select}"
    sa.event.listen(table, "after_create", sa.DDL(statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]
    sa.event.listen(
        table.metadata,
        "before_drop",
        sa.DDL(f"DROP VIEW IF EXISTS {name}").execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )
