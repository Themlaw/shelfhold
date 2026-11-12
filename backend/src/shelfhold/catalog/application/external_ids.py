"""External identifiers in storage (EF-BIB-003): add, never overwrite, never delete.

The only writes are an insert and the mark of a renumbering: a provider that renumbers adds the
new value and marks the old one `superseded` (`origin = legacy`); the old value still names its
entity. There is no update and no delete here, and the database refuses them anyway (triggers of
migration 0026). The caller owns the transaction.
"""

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.domain.external_ids import ExternalId, normalize_external_id
from shelfhold.infrastructure.schema import catalog

__all__ = [
    "ExternalIdConflictError",
    "StoredExternalId",
    "add_external_id",
    "external_ids_of",
    "holder_of",
    "renumber_external_id",
]


class ExternalIdConflictError(Exception):
    """The value already names another entity of that type: a suggestion, never an overwrite."""

    def __init__(self, external: ExternalId, holder_id: str) -> None:
        super().__init__(f"{external.entity_type}:{external.provider} already names {holder_id}")
        self.external = external
        self.holder_id = holder_id


@dataclass(frozen=True)
class StoredExternalId:
    """An identifier as stored, with its origin and state."""

    external: ExternalId
    origin: str
    state: str  # current | superseded


def _clean(external: ExternalId) -> ExternalId:
    return normalize_external_id(
        external.entity_type, external.provider, external.value, id_scheme=external.id_scheme
    )


def holder_of(connection: Connection, external: ExternalId) -> str | None:
    """The entity this value names, current or superseded; `None` when it names none."""
    clean, table = _clean(external), catalog.external_id
    found = connection.execute(
        sa.select(table.c.entity_id).where(
            table.c.entity_type == clean.entity_type,
            table.c.provider == clean.provider,
            table.c.value == clean.value,
        )
    ).scalar()
    return None if found is None else str(found)


def add_external_id(
    connection: Connection, entity_id: str, external: ExternalId, *, origin: str
) -> bool:
    """Record that `external` names `entity_id`. `True` when added, `False` when it already did;
    `ExternalIdConflictError` when it names another entity (nothing is written)."""
    clean = _clean(external)
    holder = holder_of(connection, clean)
    if holder == entity_id:
        return False
    if holder is not None:
        raise ExternalIdConflictError(clean, holder)
    connection.execute(
        sa.insert(catalog.external_id).values(
            entity_type=clean.entity_type,
            entity_id=entity_id,
            provider=clean.provider,
            value=clean.value,
            id_scheme=clean.id_scheme,
            origin=origin,
        )
    )
    return True


def renumber_external_id(
    connection: Connection, entity_id: str, old: ExternalId, new_value: str, *, origin: str
) -> None:
    """A provider renumbered: add the new value, mark the old one `superseded`, overwrite nothing.

    Applying the same renumbering again changes nothing. A value to itself, or
    back to one the entity already superseded (the table is append-only, so it could not be
    `current` again), is refused: the entity would be left with no current identifier."""
    clean_old = _clean(old)
    new = _clean(ExternalId(old.entity_type, old.provider, new_value, old.id_scheme))
    if (clean_old.entity_type, clean_old.provider, clean_old.value) == (
        new.entity_type, new.provider, new.value,
    ):  # fmt: skip
        raise ExternalIdConflictError(new, entity_id)
    table = catalog.external_id
    back = (
        connection.execute(  # the table is append-only: a superseded value is never current again
            sa.select(table.c.state).where(
                table.c.entity_type == new.entity_type,
                table.c.provider == new.provider,
                table.c.value == new.value,
                table.c.entity_id == entity_id,
            )
        ).scalar()
    )
    if back == "superseded":
        raise ExternalIdConflictError(new, entity_id)
    add_external_id(connection, entity_id, new, origin=origin)
    connection.execute(
        sa.update(table)
        .where(
            table.c.entity_type == clean_old.entity_type,
            table.c.provider == clean_old.provider,
            table.c.value == clean_old.value,
            table.c.entity_id == entity_id,
            table.c.state == "current",
        )
        .values(state="superseded", origin="legacy")
    )


def external_ids_of(
    connection: Connection, entity_type: str, entity_id: str
) -> list[StoredExternalId]:
    table = catalog.external_id
    rows = connection.execute(
        sa.select(table.c.provider, table.c.value, table.c.id_scheme, table.c.origin, table.c.state)
        .where(table.c.entity_type == entity_type, table.c.entity_id == entity_id)
        .order_by(table.c.provider, table.c.value)
    )
    return [
        StoredExternalId(
            ExternalId(entity_type, r.provider, r.value, r.id_scheme), r.origin, r.state
        )
        for r in rows
    ]
