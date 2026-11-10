"""What the registry of copies and the contents say a purge must reach (EF-BIB-052).

The library owns the storage tables, so the purge (an export-side orchestration, because it also
touches the manifest, the mirror and the tickets) reads them only through here and through
`library.replica` (the one door to the registry of copies). A copy is one `replica` row (a file
under a physical root) or one `trash_item` (an entry of a root's bin). Nothing here touches a
disk: it names paths, the purge deletes them.
"""

from collections.abc import Collection

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import platform, storage
from shelfhold.library.adapters.meta_projection import meta_rel
from shelfhold.library.adapters.origin_comicinfo import origin_rel
from shelfhold.library.domain.content_hash import CONTENT_HASH_BY, FILE_HASH_BY
from shelfhold.library.domain.copies import Copy, Hash
from shelfhold.library.replica import erase_replicas, replica_copies, replica_hashes

_content, _trash, _root = storage.content, storage.trash_item, platform.physical_root

__all__ = [
    "Copy",
    "Hash",
    "content_hashes",
    "content_ids",
    "copies",
    "derived_rels",
    "erase_content_rows",
    "unresolved_bin_items",
]


def copies(
    connection: Connection, unit_ids: Collection[str], export_item_ids: Collection[str]
) -> list[Copy]:
    """Every file copy of these Units and export items, and every bin entry that holds one."""
    found = replica_copies(connection, unit_ids, export_item_ids)
    bin_owner = sa.or_(
        sa.and_(_trash.c.kind == "unit", _trash.c.ref.in_(unit_ids)),
        sa.and_(_trash.c.kind == "export_file", _trash.c.ref.in_(export_item_ids)),
    )
    for row in connection.execute(
        sa.select(_trash, _root.c.path_entry, _root.c.mode)
        .select_from(_trash.join(_root, _root.c.root_id == _trash.c.physical_root_id))
        .where(bin_owner)
        .order_by(_trash.c.id)
    ):
        entry = row.path.split("/", 1)[0]
        unit = row.ref if row.kind == "unit" else None
        item = row.ref if row.kind == "export_file" else None
        found.append(
            Copy("bin_entry", row.id, "trash", row.physical_root_id, row.path_entry, row.mode,
                 entry, unit, item)
        )  # fmt: skip
    return found


def unresolved_bin_items(connection: Connection, unit_ids: Collection[str]) -> int:
    """Bin items of these Units that sit in no root: they cannot be located, so they are
    reported and never guessed."""
    return int(
        connection.execute(
            sa.select(sa.func.count())
            .select_from(_trash)
            .where(
                _trash.c.kind == "unit",
                _trash.c.ref.in_(unit_ids),
                _trash.c.physical_root_id.is_(None),
            )
        ).scalar_one()
    )


def content_hashes(
    connection: Connection, unit_ids: Collection[str]
) -> tuple[list[Hash], list[Hash]]:
    """(payload hashes, derived hashes) of these Units, each with the algorithm that made it: what
    a `variant` tombstone cites, so the same file is refused wherever it comes back from."""
    payload: dict[tuple[str, str], Hash] = {}
    derived: dict[tuple[str, str], Hash] = {}
    file_rows = []
    for row in connection.execute(
        sa.select(
            _content.c.original_sha256, _content.c.content_hash, _content.c.content_hash_by,
            _content.c.derived_hash, _content.c.derived_hash_by, _content.c.derived_by,
        ).where(_content.c.unit_id.in_(unit_ids))
    ):  # fmt: skip
        file_rows.append(row)
        if row.content_hash:
            by = row.content_hash_by or CONTENT_HASH_BY
            payload[(by, row.content_hash)] = Hash(by, row.content_hash)
        if row.derived_hash:
            by = row.derived_hash_by or row.derived_by or CONTENT_HASH_BY
            derived[(by, row.derived_hash)] = Hash(by, row.derived_hash)
    for value in {row.original_sha256 for row in file_rows}:
        payload.setdefault((FILE_HASH_BY, value), Hash(FILE_HASH_BY, value))
    by_content, by_file = replica_hashes(connection, unit_ids)
    for value in by_content:
        payload.setdefault((CONTENT_HASH_BY, value), Hash(CONTENT_HASH_BY, value))
    for value in by_file:  # the rendered file of a destination: what an adoption would see
        payload.setdefault((FILE_HASH_BY, value), Hash(FILE_HASH_BY, value))
    return list(payload.values()), list(derived.values())


def erase_content_rows(
    connection: Connection, unit_ids: Collection[str], export_item_ids: Collection[str]
) -> None:
    """The storage rows of these Units: contents, copies, bin items. Run once the files are gone
    (nothing cascades from `content`, so a file is never forgotten on disk)."""
    connection.execute(sa.delete(_content).where(_content.c.unit_id.in_(unit_ids)))
    erase_replicas(connection, unit_ids, export_item_ids)
    connection.execute(
        sa.delete(_trash).where(
            sa.or_(
                sa.and_(_trash.c.kind == "unit", _trash.c.ref.in_(unit_ids)),
                sa.and_(_trash.c.kind == "export_file", _trash.c.ref.in_(export_item_ids)),
            )
        )
    )


def content_ids(connection: Connection, unit_ids: Collection[str]) -> list[str]:
    return list(
        connection.execute(
            sa.select(_content.c.id).where(_content.c.unit_id.in_(unit_ids))
        ).scalars()
    )


DERIVED_CACHES = ("cache/thumbnails", "cache/content")  # under /data, keyed by Unit


def derived_rels(unit_id: str) -> list[str]:
    """Paths under /data that hold something derived from a Unit: its `unit.json`, the ComicInfo
    the service sent, its thumbnails
    and its `/content` cache (`cache/<kind>/<id[:2]>/<id>`, like `meta/units`). A purge erases
    them; the writers of those caches follow the same layout."""
    if not is_ulid(unit_id):
        raise ValueError(f"not an identifier: {unit_id!r}")
    return [
        meta_rel(unit_id),
        origin_rel(unit_id),  # the ComicInfo as the service sent it (EF-BIB-048)
        *(f"{base}/{unit_id[:2]}/{unit_id}" for base in DERIVED_CACHES),
    ]
