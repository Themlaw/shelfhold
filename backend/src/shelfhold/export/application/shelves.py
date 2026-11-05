"""Shelves and their bindings to destinations (EF-EXP-002, EF-EXP-058).

The rules are the pure ones of `domain.bindings`; this adds what they need from the database:
the other bindings of the instance, and the folder a `tree` binding writes into.
"""

import os

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application.snapshot import load_context
from shelfhold.export.domain.bindings import (
    WRITING_TRANSPORTS,
    BindingFacts,
    Egress,
    check_binding,
    check_shelf_ceiling,
    resolve_egress,
)
from shelfhold.export.domain.naming import DEFAULT_TEMPLATE
from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMode, clean_absolute
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export

__all__ = ["ShelfService"]


class ShelfService:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_shelf(
        self,
        *,
        name: str,
        phrase: str,
        max_age_rating: int | None = None,
        retention_mode: str = "passthrough",
    ) -> str:
        """Only an adult shelf in M1a: a ceiling below 18 is refused before anything is stored."""
        check_shelf_ceiling(max_age_rating)
        shelf_id = new_ulid()
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(export.shelf).values(
                    id=shelf_id, name=name, phrase=phrase, max_age_rating=max_age_rating,
                    retention_mode=retention_mode,
                )
            )  # fmt: skip
        return shelf_id

    def bind_destination(
        self,
        *,
        shelf_id: str,
        target_id: str,
        transport: str,
        plugin_egress: Egress,
        attested_egress: Egress | None = None,
        root_subpath: str = "",
        library_type: str = "manga",
    ) -> str:
        """Link a shelf to a destination; the egress is what the instance attests, else the
        plugin's default. Refused: overlapping `tree` roots, a `pull` without a readable copy."""
        destination_id = new_ulid()
        with self._engine.begin() as connection:
            shelf = connection.execute(
                sa.select(export.shelf.c.retention_mode).where(export.shelf.c.id == shelf_id)
            ).one()
            raw = connection.execute(
                sa.select(export.export_target.c.path_map).where(
                    export.export_target.c.id == target_id
                )
            ).scalar_one()
            entries = PathMap(
                tuple(
                    PathEntry(e["local"], e["remote"], PathMode(e.get("mode", "rw"))) for e in raw
                )
            ).writable()
            root = None
            if transport in WRITING_TRANSPORTS and entries:
                root = clean_absolute(
                    f"{entries[0].local}/{root_subpath}" if root_subpath else entries[0].local
                )
            real, identity = resolve_folder(root)
            new = BindingFacts(
                destination_id, shelf_id, target_id, transport,
                resolve_egress(plugin_default=plugin_egress, attested=attested_egress), real,
                identity,
            )  # fmt: skip
            check_binding(new, self._existing(connection), retention_mode=shelf.retention_mode)
            connection.execute(
                sa.insert(export.shelf_destination).values(
                    id=destination_id, shelf_id=shelf_id, target_id=target_id,
                    transport=transport, egress=new.egress, root_subpath=root_subpath,
                    template=DEFAULT_TEMPLATE.dumps(),
                    template_origin=f"default@{DEFAULT_TEMPLATE.version}",
                    kavita_library_type=library_type,
                )
            )  # fmt: skip
        return destination_id

    def _existing(self, connection: sa.Connection) -> list[BindingFacts]:
        found = []
        for row in connection.execute(sa.select(export.shelf_destination.c.id)):
            ctx = load_context(connection, row.id)
            root = ctx.root_local if ctx.transport in WRITING_TRANSPORTS else None
            real, identity = resolve_folder(root)
            found.append(
                BindingFacts(
                    ctx.destination_id,
                    ctx.shelf_id,
                    ctx.target_id,
                    ctx.transport,
                    ctx.egress,
                    real,
                    identity,
                )
            )
        return found


def resolve_folder(path: str | None) -> tuple[str | None, tuple[int, int] | None]:
    """The real path of a folder (links followed, so an alias is the same folder) and its
    `(st_dev, st_ino)` when it exists (so are a bind mount's or a case-insensitive twin)."""
    if path is None:
        return None, None
    real = os.path.realpath(path)
    try:
        observed = os.stat(real)
    except FileNotFoundError:
        return real, None
    return real, (observed.st_dev, observed.st_ino)
