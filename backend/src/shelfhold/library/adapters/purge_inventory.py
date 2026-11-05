"""What the registry of copies and the contents say a purge must reach (EF-BIB-052).

The library owns the storage tables, so the purge (an export-side orchestration, because it also
touches the manifest, the mirror and the tickets) reads and erases them only through here. A copy
is one `replica` row (a file under a physical root, or an upstream claim) or one `trash_item` (an
entry of a root's bin). Nothing here touches a disk: it names paths, the purge deletes them.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import platform, storage
from shelfhold.library.adapters.meta_projection import meta_rel
from shelfhold.library.domain.content_hash import CONTENT_HASH_BY

_replica, _content, _trash, _root = (
    storage.replica,
    storage.content,
    storage.trash_item,
    platform.physical_root,
)


@dataclass(frozen=True)
class Copy:
    """A copy to erase: a file (`rel_path` under `root_path`) or a bin entry (`rel_path` is the
    entry folder under the root's `.shelfhold/recycle`)."""

    kind: str  # `file` | `bin_entry`
    row_id: str
    location: str  # library | destination | store | adopted_ro | trash
    root_id: str
    root_path: str
    root_mode: str  # rw | adopt_ro
    rel_path: str
    unit_id: str | None
    export_item_id: str | None


@dataclass(frozen=True)
class Hash:
    by: str
    value: str


def copies(
    connection: Connection, unit_ids: Collection[str], export_item_ids: Collection[str]
) -> list[Copy]:
    """Every file copy of these Units and export items, and every bin entry that holds one.
    Upstream claims are not files: see `upstream_claims`."""
    found: list[Copy] = []
    owner = sa.or_(_replica.c.unit_id.in_(unit_ids), _replica.c.export_item_id.in_(export_item_ids))
    for row in connection.execute(
        sa.select(_replica, _root.c.path_entry, _root.c.mode)
        .select_from(_replica.join(_root, _root.c.root_id == _replica.c.physical_root_id))
        .where(owner, _replica.c.rel_path.is_not(None))
        .order_by(_replica.c.id)
    ):
        found.append(
            Copy("file", row.id, row.location, row.physical_root_id, row.path_entry, row.mode,
                 row.rel_path, row.unit_id, row.export_item_id)
        )  # fmt: skip
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
    """Bin items of these Units that sit in no root (the bin of `/store` is a root's like any
    other: a row without a root cannot be located and is reported, never guessed)."""
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


def upstream_claims(connection: Connection, unit_ids: Collection[str]) -> int:
    """The copies Shelfhold counts as living upstream: the claim a purge withdraws (M1a), without
    writing to the service (EF-BIB-052, passe 12 M7)."""
    return int(
        connection.execute(
            sa.select(sa.func.count())
            .select_from(_replica)
            .where(_replica.c.unit_id.in_(unit_ids), _replica.c.location == "upstream")
        ).scalar_one()
    )


def content_hashes(
    connection: Connection, unit_ids: Collection[str]
) -> tuple[list[Hash], list[Hash]]:
    """(payload hashes, derived hashes) of these Units, each with the algorithm that made it: what
    a `variant` tombstone cites, so the same file is refused wherever it comes back from."""
    payload: dict[tuple[str, str], Hash] = {}
    derived: dict[tuple[str, str], Hash] = {}
    for row in connection.execute(
        sa.select(
            _content.c.content_hash, _content.c.content_hash_by, _content.c.derived_hash,
            _content.c.derived_hash_by, _content.c.derived_by,
        ).where(_content.c.unit_id.in_(unit_ids))
    ):  # fmt: skip
        if row.content_hash:
            by = row.content_hash_by or CONTENT_HASH_BY
            payload[(by, row.content_hash)] = Hash(by, row.content_hash)
        if row.derived_hash:
            by = row.derived_hash_by or row.derived_by or CONTENT_HASH_BY
            derived[(by, row.derived_hash)] = Hash(by, row.derived_hash)
    for value in connection.execute(
        sa.select(_replica.c.content_hash).where(
            _replica.c.unit_id.in_(unit_ids), _replica.c.content_hash.is_not(None)
        )
    ).scalars():
        payload.setdefault((CONTENT_HASH_BY, value), Hash(CONTENT_HASH_BY, value))
    return list(payload.values()), list(derived.values())


def erase_rows(
    connection: Connection, unit_ids: Sequence[str], export_item_ids: Sequence[str]
) -> None:
    """The storage rows of these Units: contents, copies, bin items. Run once the files are gone
    (nothing cascades from `content`, so a file is never forgotten on disk)."""
    connection.execute(sa.delete(_content).where(_content.c.unit_id.in_(unit_ids)))
    connection.execute(
        sa.delete(_replica).where(
            sa.or_(_replica.c.unit_id.in_(unit_ids), _replica.c.export_item_id.in_(export_item_ids))
        )
    )
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
    """Paths under /data that hold something derived from a Unit: its `unit.json`, its thumbnails
    and its `/content` cache (`cache/<kind>/<id[:2]>/<id>`, like `meta/units`). A purge erases
    them; the writers of those caches follow the same layout."""
    if not is_ulid(unit_id):
        raise ValueError(f"not an identifier: {unit_id!r}")
    return [meta_rel(unit_id), *(f"{base}/{unit_id[:2]}/{unit_id}" for base in DERIVED_CACHES)]


def withdraw_upstream_claims(connection: Connection, unit_ids: Collection[str]) -> None:
    """Shelfhold no longer counts the upstream copy as its own (M1a: nothing is written there)."""
    connection.execute(
        sa.delete(_replica).where(
            _replica.c.unit_id.in_(unit_ids), _replica.c.location == "upstream"
        )
    )
