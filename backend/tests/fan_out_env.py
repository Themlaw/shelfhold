"""One shelf, several bindings (M4 L06a, EF-EXP-002, ADR-0052).

Built on `export_env`: the shelf and its first binding are the ones of the synthetic library; the
helpers below add what the fan-out needs (another server, another folder of the same library, a
remote library to index, a target that keeps no accounts apart). Nothing is faked but the clock and
the `plugin/parse` replica of `export_env`.
"""

import sqlalchemy as sa

from shelfhold.export.application.shelves import ShelfService
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv
from tests.family_env import SAFE_KAVITA


def add_target(
    env: ExportEnv, *, kind: str = "kavita", partitioning: str = "library", name: str | None = None
) -> str:
    """Another server writing into the same library (its path map is the one of the first). A
    Kavita always partitions by library; any other kind counts what it declares only once the
    admin attested it, which is done here unless the value is `none`."""
    target_id = new_ulid()
    path_map = [{"local": str(env.lib.manga), "remote": "/manga", "mode": "rw"}]
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(export.export_target).values(
                id=target_id, kind=kind, name=name or f"Server {target_id[-6:].lower()}",
                path_map=path_map, access_partitioning=partitioning, version_seen=SAFE_KAVITA,
                access_partitioning_attested_at=env.clock.now() if partitioning != "none" else None,
            )
        )  # fmt: skip
    return target_id


def link_library(env: ExportEnv, destination_id: str, target_id: str) -> None:
    """The remote library a `tree` binding indexes (a binding without one plans nothing)."""
    library_id = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(export.remote_library).values(
                id=library_id, export_target_id=target_id, remote_ref=library_id[-4:]
            )
        )
        connection.execute(
            sa.insert(export.shelf_destination_remote_library).values(
                shelf_destination_id=destination_id,
                remote_library_id=library_id,
                export_target_id=target_id,
            )
        )


def service(env: ExportEnv) -> ShelfService:
    return ShelfService(env.engine, clock=env.clock)


def bind(
    env: ExportEnv,
    subpath: str,
    *,
    target_id: str | None = None,
    shelf_id: str | None = None,
    attach_to_tree: str | None = None,
    link: bool = True,
) -> str:
    """Bind the shelf to a folder of another (or the same) server, through the service."""
    target = target_id or add_target(env)
    if attach_to_tree is None:
        (env.lib.manga / subpath).mkdir(exist_ok=True)
    destination_id = service(env).bind_destination(
        shelf_id=shelf_id or env.shelf_id, target_id=target, transport="tree",
        plugin_egress="foyer", root_subpath=subpath, attach_to_tree=attach_to_tree,
    )  # fmt: skip
    if link:
        link_library(env, destination_id, target)
    return destination_id


def tree_of(env: ExportEnv, destination_id: str) -> str | None:
    with env.engine.connect() as connection:
        return connection.execute(
            sa.select(export.tree_indexer.c.tree_id).where(
                export.tree_indexer.c.shelf_destination_id == destination_id
            )
        ).scalar()


def records_of(env: ExportEnv, destination_id: str) -> list[sa.Row[tuple[object, ...]]]:
    table = export.export_record
    with env.engine.connect() as connection:
        return list(
            connection.execute(
                sa.select(table)
                .where(table.c.shelf_destination_id == destination_id)
                .order_by(table.c.export_item_id)
            )
        )


def set_partitioning(env: ExportEnv, target_id: str, value: str) -> None:
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.update(export.export_target)
            .where(export.export_target.c.id == target_id)
            .values(access_partitioning=value, access_partitioning_attested_at=None)
        )
