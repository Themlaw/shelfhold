"""The rows the headless facade reads and writes: every query of `shelfhold config|target|source|
shelf` lives here, as a function of a connection (the caller owns the transaction, because a
sealed secret must land in the same one). `HeadlessAdmin` itself asks for no SQL (EF-INT-024)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.catalog.public import record_removed
from shelfhold.infrastructure.schema import acquisition, export, identity, platform

SUWAYOMI = "suwayomi"


def wake_schedule(connection: Connection, kinds: Collection[str], now: datetime) -> None:
    table = platform.schedule
    connection.execute(
        sa.update(table).where(table.c.job_kind.in_(kinds), table.c.enabled).values(next_due_at=now)
    )


def target_path_maps(connection: Connection) -> list[Any]:
    return list(
        connection.execute(sa.select(export.export_target.c.id, export.export_target.c.path_map))
    )


def physical_roots(connection: Connection) -> list[Any]:
    table = platform.physical_root
    return list(connection.execute(sa.select(table.c.path_entry, table.c.mode)))


def target_names(connection: Connection) -> list[str]:
    table = export.export_target
    return list(connection.execute(sa.select(table.c.name).order_by(table.c.name)).scalars())


def set_jurisdiction(connection: Connection, value: str, confirmed_at: datetime) -> None:
    """The admin chose the country: it is confirmed from now on (EF-SRC-024, D4)."""
    connection.execute(
        sa.update(platform.instance).values(
            jurisdiction=value, jurisdiction_confirmed_at=confirmed_at
        )
    )


def setting(connection: Connection, key: str) -> str | None:
    table = platform.app_setting
    return connection.execute(
        sa.select(table.c.value).where(table.c.key == key)
    ).scalar_one_or_none()


def write_setting(connection: Connection, key: str, value: str) -> None:
    statement = sqlite_insert(platform.app_setting).values(key=key, value=value)
    connection.execute(
        statement.on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )


def target_named(connection: Connection, name: str) -> Any:
    table = export.export_target
    return connection.execute(sa.select(table).where(table.c.name == name)).first()


def kavita_target(connection: Connection, id_or_name: str) -> Any:
    table = export.export_target
    return connection.execute(
        sa.select(table).where(
            sa.or_(table.c.id == id_or_name, table.c.name == id_or_name), table.c.kind == "kavita"
        )
    ).first()


def set_path_map(
    connection: Connection, target_id: str, entries: Sequence[Any], now: datetime
) -> None:
    table = export.export_target
    connection.execute(
        sa.update(table).where(table.c.id == target_id).values(path_map=entries, updated_at=now)
    )


def insert_kavita_target(
    connection: Connection,
    *,
    target_id: str,
    name: str,
    config: dict[str, Any],
    path_map: Sequence[Any],
    version_seen: str | None,
) -> None:
    connection.execute(
        sa.insert(export.export_target).values(
            id=target_id, kind="kavita", name=name, config=config, path_map=path_map,
            version_seen=version_seen,
        )
    )  # fmt: skip


def set_target_config(connection: Connection, target_id: str, config: dict[str, Any]) -> None:
    table = export.export_target
    connection.execute(sa.update(table).where(table.c.id == target_id).values(config=config))


def insert_audit(
    connection: Connection,
    *,
    audit_id: str,
    actor_id: str | None,
    action: str,
    target: str,
    at: datetime,
    session_id: str | None,
    auth_time: datetime | None,
) -> None:
    connection.execute(
        sa.insert(identity.audit).values(
            id=audit_id, actor_id=actor_id, action=action, target=target, at=at,
            session_id=session_id, auth_time=auth_time,
        )
    )  # fmt: skip


def source_row(connection: Connection) -> Any:
    """The settings and the sealed-secret id of the Suwayomi source, or None."""
    table = acquisition.source_config
    return connection.execute(
        sa.select(table.c.settings, table.c.secret_kid).where(table.c.plugin_id == SUWAYOMI)
    ).first()


def insert_source(connection: Connection, settings: dict[str, Any]) -> None:
    connection.execute(
        sa.insert(acquisition.source_config).values(plugin_id=SUWAYOMI, settings=settings)
    )


def update_source(connection: Connection, settings: dict[str, Any], now: datetime) -> None:
    table = acquisition.source_config
    connection.execute(
        sa.update(table)
        .where(table.c.plugin_id == SUWAYOMI)
        .values(settings=settings, updated_at=now)
    )


def shelf_named(connection: Connection, name: str) -> Any:
    table = export.shelf
    return connection.execute(
        sa.select(table.c.id, table.c.max_age_rating).where(table.c.name == name)
    ).first()


def shelf_binding(connection: Connection, shelf_id: str, target_id: str) -> Any:
    table = export.shelf_destination
    return connection.execute(
        sa.select(table.c.id, table.c.root_subpath)
        .where(table.c.shelf_id == shelf_id)
        .where(table.c.target_id == target_id)
    ).first()


def bound_subpath(connection: Connection, shelf_id: str, target_name: str) -> str | None:
    table = export.shelf_destination
    return connection.execute(
        sa.select(table.c.root_subpath)
        .join(export.export_target, export.export_target.c.id == table.c.target_id)
        .where(table.c.shelf_id == shelf_id, export.export_target.c.name == target_name)
    ).scalar_one_or_none()


def unbind(connection: Connection, shelf_id: str, destination_id: str, *, drop_shelf: bool) -> None:
    connection.execute(
        sa.delete(export.shelf_destination).where(export.shelf_destination.c.id == destination_id)
    )
    if drop_shelf:
        record_removed(connection, "shelf", shelf_id)  # `GET /shelves?since=` tells the removal
        connection.execute(sa.delete(export.shelf).where(export.shelf.c.id == shelf_id))
