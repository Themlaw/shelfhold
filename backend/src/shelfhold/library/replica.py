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

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export, platform, storage
from shelfhold.library.domain.copies import Copy

__all__ = [
    "LibraryCopy",
    "StoreCopy",
    "demote_shared_inodes",
    "distrust_root_copies",
    "StoreProof",
    "erase_replicas",
    "has_readable_copy",
    "independent_copies",
    "kept_file_hashes",
    "library_copies",
    "manifest_file_hashes",
    "median_unit_bytes",
    "paths_under",
    "readable_unit_ids",
    "record_library_rewrite",
    "record_replica",
    "record_store_observation",
    "reference_content_hash",
    "relocate_destination_copy",
    "relocate_root_paths",
    "rendered_file_hashes",
    "replica_copies",
    "replica_hashes",
    "sibling_inodes",
    "store_copies",
    "store_unit_ids",
    "store_proofs",
    "update_observation",
    "upstream_claims",
    "withdraw_upstream_claims",
]

replica_table = storage.replica
_ORIGINAL_PLACES = ("library", "destination")
# Where a file is readable by Shelfhold itself: its library, its store, an adopted read-only
# root. A destination copy or the upstream is not "a file we hold" (I20, EF-BIB-044).
_READABLE_PLACES = ("library", "store", "adopted_ro")
# The record of a destination says the file is there (same list as `present_in`).
_RECORD_PRESENT = ("written", "scanned", "resolved", "adopted")


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


def paths_under(
    connection: Connection, root_id: str, folder: str
) -> list[tuple[str, int | None, str | None]]:
    """`(rel_path, size, file_hash)` of the copies of a root that sit directly under `folder`."""
    rows = connection.execute(
        sa.select(replica_table.c.rel_path, replica_table.c.size, replica_table.c.file_hash).where(
            replica_table.c.physical_root_id == root_id,
            replica_table.c.rel_path.like(f"{folder}/%"),
        )
    ).all()
    return [(p, size, h) for p, size, h in rows if p and "/" not in p[len(folder) + 1 :]]


def relocate_root_paths(
    connection: Connection, root_id: str, moves: Sequence[tuple[str, str]]
) -> int:
    """The copies of a root follow the files that were really moved on disk: `(old, new)` paths
    relative to the root, one row each. Idempotent: a row already moved no longer matches."""
    done = 0
    for old, new in moves:
        done += connection.execute(
            sa.update(replica_table)
            .where(replica_table.c.physical_root_id == root_id, replica_table.c.rel_path == old)
            .values(rel_path=new)
        ).rowcount
    return done


def forget_destination_copies(connection: Connection, export_item_id: str) -> None:
    """The stale `destination` rows of an item that is written again (EF-EXP-018)."""
    connection.execute(
        sa.delete(replica_table).where(
            replica_table.c.export_item_id == export_item_id,
            replica_table.c.location == "destination",
        )
    )


def destination_identities(
    connection: Connection, export_item_ids: Sequence[str]
) -> dict[str, tuple[int | None, int | None]]:
    """`(dev, ino)` recorded for the `destination` copy of each item: an atomic replacement of
    the file by a third party changes the inode while size and mtime may stay (EF-EXP-043)."""
    rows = connection.execute(
        sa.select(replica_table.c.export_item_id, replica_table.c.dev, replica_table.c.ino).where(
            replica_table.c.export_item_id.in_(export_item_ids),
            replica_table.c.location == "destination",
        )
    )
    return {r.export_item_id: (r.dev, r.ino) for r in rows}


def refresh_destination_identity(
    connection: Connection,
    export_item_id: str,
    *,
    dev: int,
    ino: int,
    size: int,
    mtime_ns: int,
    verified_at: datetime,
) -> None:
    """The hash confirmed that the file is the same one: its new identity becomes the baseline."""
    connection.execute(
        sa.update(replica_table)
        .where(
            replica_table.c.export_item_id == export_item_id,
            replica_table.c.location == "destination",
        )
        .values(dev=dev, ino=ino, size=size, mtime_ns=mtime_ns, verified_at=verified_at)
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


@dataclass(frozen=True)
class StoreCopy:
    replica_id: str
    local: str
    file_hash: str | None  # the bytes that were put in /store


def store_copies(connection: Connection, unit_id: str) -> Sequence[StoreCopy]:
    """The Unit's copies in /store, oldest first (the kept originals, EF-BIB-027)."""
    root = platform.physical_root
    rows = connection.execute(
        sa.select(
            replica_table.c.id,
            replica_table.c.rel_path,
            replica_table.c.file_hash,
            root.c.path_entry,
        )
        .join(root, root.c.root_id == replica_table.c.physical_root_id)
        .where(replica_table.c.unit_id == unit_id, replica_table.c.location == "store")
        .order_by(replica_table.c.id)
    )
    return [StoreCopy(r.id, f"{r.path_entry.rstrip('/')}/{r.rel_path}", r.file_hash) for r in rows]


@dataclass(frozen=True)
class StoreProof:
    """What a verified copy in /store looks like: the identity and the hashes a later deed pins."""

    replica_id: str
    local: str
    file_hash: str  # the bytes that were put in /store
    content_hash: str  # the payload they carry
    dev: int
    ino: int


def store_proofs(connection: Connection, unit_id: str) -> list[StoreProof]:
    """The Unit's /store copies that count as independent, with their identity. Read from the
    registry as the last look (`verify_kept`) left it: a caller about to act on it looks again."""
    root = platform.physical_root
    rows = connection.execute(
        sa.select(
            replica_table.c.id,
            replica_table.c.rel_path,
            replica_table.c.file_hash,
            replica_table.c.content_hash,
            replica_table.c.dev,
            replica_table.c.ino,
            root.c.path_entry,
        )
        .join(root, root.c.root_id == replica_table.c.physical_root_id)
        .where(
            replica_table.c.unit_id == unit_id,
            replica_table.c.location == "store",
            replica_table.c.independent,
            replica_table.c.file_hash.is_not(None),
            replica_table.c.content_hash.is_not(None),
            replica_table.c.dev.is_not(None),
            replica_table.c.ino.is_not(None),
        )
        .order_by(replica_table.c.id)
    )
    return [
        StoreProof(
            r.id,
            f"{r.path_entry.rstrip('/')}/{r.rel_path}",
            r.file_hash,
            r.content_hash,
            int(r.dev),
            int(r.ino),
        )
        for r in rows
    ]


def kept_file_hashes(connection: Connection) -> dict[str, str]:
    """Unit -> the hash of the file in /store, for the Units that have one (the kept originals)."""
    rows = connection.execute(
        sa.select(replica_table.c.unit_id, replica_table.c.file_hash)
        .where(
            replica_table.c.location == "store",
            replica_table.c.unit_id.is_not(None),
            replica_table.c.file_hash.is_not(None),
        )
        .order_by(replica_table.c.id)
    )
    return {r.unit_id: r.file_hash for r in rows}


def sibling_inodes(connection: Connection, unit_id: str) -> set[tuple[int, int]]:
    """`(dev, ino)` of the Unit's other copies on disk (library, destinations, adopted files):
    a /store file that is one of them is not a copy of its own."""
    rows = connection.execute(
        sa.select(replica_table.c.dev, replica_table.c.ino).where(
            replica_table.c.unit_id == unit_id,
            replica_table.c.location != "store",
            replica_table.c.location != "upstream",
            replica_table.c.dev.is_not(None),
            replica_table.c.ino.is_not(None),
        )
    )
    return {(r.dev, r.ino) for r in rows}


def record_library_rewrite(
    connection: Connection,
    replica_id: str,
    *,
    file_hash: str,
    dev: int,
    ino: int,
    size: int,
    mtime_ns: int,
    verified_at: datetime,
) -> None:
    """A /library file replaced in place (an atomic write makes a new inode): the manifest's hash
    and the identity of what is there now, so a stale inode is never counted as a copy."""
    connection.execute(
        sa.update(replica_table)
        .where(replica_table.c.id == replica_id, replica_table.c.location == "library")
        .values(
            file_hash=file_hash,
            dev=dev,
            ino=ino,
            size=size,
            mtime_ns=mtime_ns,
            verified_at=verified_at,
        )
    )


def record_store_observation(
    connection: Connection,
    replica_id: str,
    *,
    independent: bool,
    dev: int | None,
    ino: int | None,
    size: int | None,
    mtime_ns: int | None,
    verified_at: datetime,
) -> None:
    """What a look at a /store file found: its inode and whether it counts as a copy of its own."""
    connection.execute(
        sa.update(replica_table)
        .where(replica_table.c.id == replica_id, replica_table.c.location == "store")
        .values(
            independent=independent,
            dev=dev,
            ino=ino,
            size=size,
            mtime_ns=mtime_ns,
            verified_at=verified_at,
        )
    )


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
    """Units with a readable copy of their own, or covered by an item that has one. A file the
    admin already had in Kavita and Shelfhold adopted is a carrier that is there (I20): it is
    referenced where it is, as a copy of the destination, and counts as long as it is not gone."""
    coverage, item = export.unit_coverage, export.export_item
    own = sa.select(replica_table.c.unit_id.label("unit_id")).where(
        replica_table.c.location.in_(_READABLE_PLACES), replica_table.c.unit_id.is_not(None)
    )
    record = export.export_record
    adopted_item = (
        sa.select(item.c.id)
        .select_from(item.join(record, record.c.export_item_id == item.c.id))
        .where(
            item.c.id == replica_table.c.export_item_id,
            item.c.kind == "adopted",
            record.c.state.in_(_RECORD_PRESENT),  # a record gone `missing` is no presence
        )
        .exists()
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
        .where(
            sa.or_(
                replica_table.c.location.in_(_READABLE_PLACES),
                sa.and_(replica_table.c.location == "destination", adopted_item),
            )
        )
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


# -- what a purge reads and erases (EF-BIB-052): the one door to the table stays here ------------


def _owned(unit_ids: Collection[str], export_item_ids: Collection[str]) -> sa.ColumnElement[bool]:
    return sa.or_(
        replica_table.c.unit_id.in_(unit_ids), replica_table.c.export_item_id.in_(export_item_ids)
    )


def replica_copies(
    connection: Connection, unit_ids: Collection[str], export_item_ids: Collection[str]
) -> list[Copy]:
    """Every file copy of these Units and export items. Upstream claims are not files."""
    roots = platform.physical_root
    rows = connection.execute(
        sa.select(replica_table, roots.c.path_entry, roots.c.mode)
        .select_from(replica_table.join(roots, roots.c.root_id == replica_table.c.physical_root_id))
        .where(_owned(unit_ids, export_item_ids), replica_table.c.rel_path.is_not(None))
        .order_by(replica_table.c.id)
    )
    return [
        Copy("file", r.id, r.location, r.physical_root_id, r.path_entry, r.mode, r.rel_path,
             r.unit_id, r.export_item_id)
        for r in rows
    ]  # fmt: skip


def replica_hashes(
    connection: Connection, unit_ids: Collection[str]
) -> tuple[list[str], list[str]]:
    """(payload hashes, whole-file hashes) the copies of these Units carry."""
    rows = connection.execute(
        sa.select(replica_table.c.content_hash, replica_table.c.file_hash).where(
            replica_table.c.unit_id.in_(unit_ids)
        )
    ).all()
    return [r.content_hash for r in rows if r.content_hash], [
        r.file_hash for r in rows if r.file_hash
    ]


def upstream_claims(connection: Connection, unit_ids: Collection[str]) -> int:
    """The copies Shelfhold counts as living upstream: the claim a purge withdraws (M1a), without
    writing to the service (EF-BIB-052, passe 12 M7)."""
    return int(
        connection.execute(
            sa.select(sa.func.count())
            .select_from(replica_table)
            .where(replica_table.c.unit_id.in_(unit_ids), replica_table.c.location == "upstream")
        ).scalar_one()
    )


def withdraw_upstream_claims(connection: Connection, unit_ids: Collection[str]) -> None:
    """Shelfhold no longer counts the upstream copy as its own (M1a: nothing is written there)."""
    connection.execute(
        sa.delete(replica_table).where(
            replica_table.c.unit_id.in_(unit_ids), replica_table.c.location == "upstream"
        )
    )


def relocate_destination_copy(
    connection: Connection,
    export_item_id: str,
    *,
    physical_root_id: str,
    rel_path: str,
    dev: int | None,
    ino: int,
    size: int,
    mtime_ns: int,
    verified_at: datetime,
) -> None:
    """The destination copy of an item is now this file: a verified copy made between two roots
    (repatriation, EF-EXP-051). The hashes and the item stay; nothing is deleted."""
    connection.execute(
        sa.update(replica_table)
        .where(
            replica_table.c.export_item_id == export_item_id,
            replica_table.c.location == "destination",
        )
        .values(
            physical_root_id=physical_root_id, rel_path=rel_path, dev=dev, ino=ino, size=size,
            mtime_ns=mtime_ns, verified_at=verified_at,
        )
    )  # fmt: skip


def erase_replicas(
    connection: Connection, unit_ids: Collection[str], export_item_ids: Collection[str]
) -> None:
    connection.execute(sa.delete(replica_table).where(_owned(unit_ids, export_item_ids)))


def distrust_root_copies(connection: Connection, root_id: str) -> int:
    """The files of a physical root that was replaced, lost or put back from an older copy
    (`root reinit`, `store reinit`, EF-EXP-064) are not known to be there any more: no copy of the
    root counts as independent until it has been looked at again. Only the registry changes; no
    file and no row is removed."""
    return connection.execute(
        sa.update(replica_table)
        .where(
            replica_table.c.physical_root_id == root_id,
            replica_table.c.location.in_(("library", "destination", "store")),
        )
        .values(independent=False, verified_at=None)
    ).rowcount


def store_unit_ids(connection: Connection, root_id: str) -> set[str]:
    """The Units that have a copy in the `/store` root `root_id`."""
    return set(
        connection.execute(
            sa.select(replica_table.c.unit_id)
            .where(
                replica_table.c.physical_root_id == root_id,
                replica_table.c.location == "store",
                replica_table.c.unit_id.is_not(None),
            )
            .distinct()
        ).scalars()
    )


def demote_shared_inodes(connection: Connection, unit_ids: Collection[str]) -> int:
    """After `(dev, ino)` were rebaselined (`reconcile --rebaseline-inodes`, EF-EXP-043): a copy
    that now shares its inode with another file of the same Unit is one file under two names, not
    a copy of its own (EF-BIB-027). Only ever demotes: promoting a copy needs the hash read again,
    which the safeguard does."""
    changed = 0
    for unit_id in unit_ids:
        rows = connection.execute(
            sa.select(
                replica_table.c.id,
                replica_table.c.dev,
                replica_table.c.ino,
                replica_table.c.independent,
            ).where(
                replica_table.c.unit_id == unit_id,
                replica_table.c.location.in_(("library", "destination", "store", "adopted_ro")),
                replica_table.c.dev.is_not(None),
                replica_table.c.ino.is_not(None),
            )
        ).all()
        owners: dict[tuple[int, int], list[Any]] = {}
        for row in rows:
            owners.setdefault((row.dev, row.ino), []).append(row)
        for sharing in owners.values():
            if len(sharing) > 1:
                for row in sharing:
                    if row.independent:
                        connection.execute(
                            sa.update(replica_table)
                            .where(replica_table.c.id == row.id)
                            .values(independent=False)
                        )
                        changed += 1
    return changed
