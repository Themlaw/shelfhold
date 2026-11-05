"""The registry of copies (`replica`) and its single reader, `independent_copies` (EF-BIB-045).

This module is the only one that touches the `replica` table; the import-linter contract
`replica-single-reader` and `test_independent_copies_single_source_of_truth` hold the door.
The purge (EF-BIB-038), `irreplaceable` (EF-BIB-031), the safeguard, the Health page and the
fan-out (M4) ask here and nowhere else.

`independent_copies(unit)` counts the copies of the Unit's payload: rows flagged `independent`
whose `content_hash` is the Unit's reference one (G1: never the file hash, which differs per
destination), one per distinct inode (`(dev, ino)`; two hard links are one copy). A row never
established as independent is not counted; a library file altered by a third party carries
another `content_hash` once looked at, so it is not counted either.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export, platform, storage

__all__ = [
    "LibraryCopy",
    "has_readable_copy",
    "independent_copies",
    "library_copies",
    "manifest_file_hashes",
    "median_unit_bytes",
    "readable_unit_ids",
    "record_replica",
    "reference_content_hash",
    "rendered_file_hashes",
    "update_observation",
]

replica_table = storage.replica
_ORIGINAL_PLACES = ("library", "destination")
# Where a file is readable by Shelfhold itself: its library, its store, an adopted read-only
# root. A destination copy or the upstream is not "a file we hold" (I20, EF-BIB-044).
_READABLE_PLACES = ("library", "store", "adopted_ro")


def reference_content_hash(connection: Connection, unit_id: str) -> str | None:
    """The payload hash the Unit's copies are compared to: the preferred Variant's, else any."""
    content = storage.content
    variant = catalog.unit_variant
    return connection.execute(
        sa.select(content.c.content_hash)
        .select_from(content.outerjoin(variant, variant.c.id == content.c.variant_id))
        .where(content.c.unit_id == unit_id, content.c.content_hash.is_not(None))
        .order_by(sa.func.coalesce(variant.c.preferred, False).desc(), content.c.id)
        .limit(1)
    ).scalar()


def _source_is_alive(unit_id: str) -> sa.ColumnElement[bool]:
    variant, binding = catalog.unit_variant, acquisition.source_binding
    listed = (
        sa.select(variant.c.id)
        .join(binding, binding.c.id == variant.c.source_binding_id)
        .where(variant.c.unit_id == unit_id, variant.c.removed_at.is_(None), binding.c.active)
        .exists()
    )
    still_there = (
        ~sa.select(catalog.unit.c.id)
        .where(catalog.unit.c.id == unit_id, catalog.unit.c.availability == "removed")
        .exists()
    )
    return sa.and_(listed, still_there)


def independent_copies(connection: Connection, unit_id: str) -> int:
    reference = reference_content_hash(connection, unit_id)
    if reference is None:
        return 0
    rows = connection.execute(
        sa.select(replica_table.c.id, replica_table.c.dev, replica_table.c.ino).where(
            replica_table.c.unit_id == unit_id,
            replica_table.c.independent,
            replica_table.c.content_hash == reference,
            # the origin is a copy only while it still lists the Unit: decided here, at counting
            # time, not by a row someone must remember to update
            sa.or_(replica_table.c.location != "upstream", _source_is_alive(unit_id)),
        )
    )
    keys: set[tuple[Any, ...]] = {
        (r.dev, r.ino) if r.dev is not None and r.ino is not None else ("row", r.id) for r in rows
    }
    return len(keys)


def record_replica(
    connection: Connection,
    *,
    unit_id: str,
    location: str,
    content_hash: str | None,
    file_hash: str | None = None,
    independent: bool,
    mutable_by_third_party: bool = False,
    physical_root_id: str | None = None,
    rel_path: str | None = None,
    remote_ref: str | None = None,
    dev: int | None = None,
    ino: int | None = None,
    size: int | None = None,
    mtime_ns: int | None = None,
    fidelity: str = "original",
    verified_at: datetime | None = None,
    export_item_id: str | None = None,
) -> str:
    replica_id = new_ulid()
    connection.execute(
        sa.insert(replica_table).values(
            id=replica_id,
            unit_id=unit_id,
            export_item_id=export_item_id,
            location=location,
            content_hash=content_hash,
            file_hash=file_hash,
            independent=independent,
            mutable_by_third_party=mutable_by_third_party,
            physical_root_id=physical_root_id,
            rel_path=rel_path,
            remote_ref=remote_ref,
            dev=dev,
            ino=ino,
            size=size,
            mtime_ns=mtime_ns,
            fidelity=fidelity,
            verified_at=verified_at,
        )
    )
    return replica_id


def forget_destination_copies(connection: Connection, export_item_id: str) -> None:
    """The stale `destination` rows of an item that is written again (EF-EXP-018)."""
    connection.execute(
        sa.delete(replica_table).where(
            replica_table.c.export_item_id == export_item_id,
            replica_table.c.location == "destination",
        )
    )


@dataclass(frozen=True)
class LibraryCopy:
    replica_id: str
    local: str  # absolute local path: the physical root's path plus the relative one
    file_hash: str | None  # what the manifest says
    content_hash: str | None


def library_copies(connection: Connection, unit_id: str) -> Sequence[LibraryCopy]:
    root = platform.physical_root
    rows = connection.execute(
        sa.select(
            replica_table.c.id,
            replica_table.c.rel_path,
            replica_table.c.file_hash,
            replica_table.c.content_hash,
            root.c.path_entry,
        )
        .join(root, root.c.root_id == replica_table.c.physical_root_id)
        .where(replica_table.c.unit_id == unit_id, replica_table.c.location == "library")
        .order_by(replica_table.c.id)
    )
    return [
        LibraryCopy(r.id, f"{r.path_entry.rstrip('/')}/{r.rel_path}", r.file_hash, r.content_hash)
        for r in rows
    ]


def update_observation(
    connection: Connection,
    replica_id: str,
    *,
    content_hash: str | None,
    independent: bool,
    verified_at: datetime,
    size: int | None = None,
    mtime_ns: int | None = None,
) -> None:
    """What was seen on disk. `file_hash`, the exporter's manifest, is deliberately not settable."""
    connection.execute(
        sa.update(replica_table)
        .where(replica_table.c.id == replica_id)
        .values(
            content_hash=content_hash,
            independent=independent,
            verified_at=verified_at,
            size=size,
            mtime_ns=mtime_ns,
        )
    )


def rendered_file_hashes(connection: Connection, unit_id: str, *, original: str) -> set[str]:
    """File hashes of what was rendered for a destination (never the original bytes)."""
    return {
        h
        for h in connection.execute(
            sa.select(replica_table.c.file_hash).where(
                replica_table.c.unit_id == unit_id,
                replica_table.c.location.in_(_ORIGINAL_PLACES),
                replica_table.c.file_hash.is_not(None),
            )
        ).scalars()
        if h != original
    }


def manifest_file_hashes(connection: Connection, unit_id: str) -> list[dict[str, Any]]:
    """The `file_hashes` of unit.json: one record per rendered file known to the registry."""
    rows = connection.execute(
        sa.select(
            replica_table.c.id,
            replica_table.c.location,
            replica_table.c.export_item_id,
            replica_table.c.file_hash,
            replica_table.c.content_hash,
        )
        .where(
            replica_table.c.unit_id == unit_id,
            replica_table.c.location.in_(_ORIGINAL_PLACES),
            replica_table.c.file_hash.is_not(None),
        )
        .order_by(replica_table.c.id)
    )
    return [
        {
            "replica_id": r.id,
            "location": r.location,
            "export_item_id": r.export_item_id,
            "file_hash": r.file_hash,
            "content_hash": r.content_hash,
        }
        for r in rows
    ]


def _readable_rows() -> sa.Select[tuple[str]]:
    """Units with a readable copy of their own, or covered by an item that has one."""
    coverage = export.unit_coverage
    own = sa.select(replica_table.c.unit_id.label("unit_id")).where(
        replica_table.c.location.in_(_READABLE_PLACES), replica_table.c.unit_id.is_not(None)
    )
    via_item = (
        sa.select(coverage.c.unit_id.label("unit_id"))
        .select_from(
            coverage.join(
                replica_table,
                sa.and_(
                    coverage.c.carrier_type == "export_item",
                    replica_table.c.export_item_id == coverage.c.carrier_id,
                ),
            )
        )
        .where(replica_table.c.location.in_(_READABLE_PLACES))
    )
    both = own.union(via_item).subquery()
    return sa.select(both.c.unit_id).where(both.c.unit_id.is_not(None))


def readable_unit_ids(connection: Connection, edition_id: str) -> set[str]:
    """The Units of an Edition that are present for acquisition (I20): a readable copy of their
    own, or a carrier (an adopted volume, an item) that covers them and has one."""
    both = _readable_rows().subquery()
    unit = catalog.unit
    return set(
        connection.execute(
            sa.select(unit.c.id).where(
                unit.c.edition_id == edition_id, unit.c.id.in_(sa.select(both.c.unit_id))
            )
        ).scalars()
    )


def has_readable_copy(connection: Connection, unit_id: str) -> bool:
    both = _readable_rows().subquery()
    return (
        connection.execute(
            sa.select(sa.literal(1)).where(sa.literal(unit_id).in_(sa.select(both.c.unit_id)))
        ).first()
        is not None
    )


def median_unit_bytes(connection: Connection, edition_id: str) -> int | None:
    """The typical size of a chapter archive already held in this Edition, to size an estimate."""
    sizes = list(
        connection.execute(
            sa.select(replica_table.c.size)
            .select_from(
                replica_table.join(catalog.unit, catalog.unit.c.id == replica_table.c.unit_id)
            )
            .where(
                catalog.unit.c.edition_id == edition_id,
                replica_table.c.location.in_(_READABLE_PLACES),
                replica_table.c.size.is_not(None),
            )
            .order_by(replica_table.c.size)
        ).scalars()
    )
    return sizes[len(sizes) // 2] if sizes else None
