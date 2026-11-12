"""What the export reads of the library: the received archive of a Unit and its hashes.

The export context never opens the storage tables or the registry of copies; it asks here
(EF-BIB-045), and records the file it wrote through `record_destination_copy`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.library.replica import forget_destination_copies, library_copies, record_replica

BIN_RETENTION = timedelta(days=30)  # ENF-OPS-009: what the bin of a root keeps


@dataclass(frozen=True)
class ExportSource:
    unit_id: str
    content_id: str
    content_hash: str | None
    original_sha256: str
    watermarked: bool
    page_count: int | None
    local_path: str | None  # the library copy to read, if any
    variant_id: str | None = None  # the Variant this archive was received for
    content_hash_by: str | None = None  # the function of `content_hash` (provenance mirror)
    derived_hash: str | None = None
    derived_by: str | None = None


def export_sources(
    connection: Connection, unit_ids: Sequence[str], *, formats: Sequence[str] = ("cbz",)
) -> dict[str, ExportSource]:
    """The archive to export for each Unit: the preferred Variant's, else the first received.

    A CBZ by default; the export pipeline also asks for an EPUB (M2 L09), whose file to read is
    then the one `servable_epub` names, never the `local_path` given here."""
    if not unit_ids:
        return {}
    content, variant = storage.content, catalog.unit_variant
    rows = connection.execute(
        sa.select(
            content.c.unit_id,
            content.c.id.label("content_id"),
            content.c.content_hash,
            content.c.original_sha256,
            content.c.watermarked,
            content.c.page_count,
            content.c.variant_id,
            content.c.content_hash_by,
            content.c.derived_hash,
            content.c.derived_by,
        )
        .select_from(content.outerjoin(variant, variant.c.id == content.c.variant_id))
        .where(content.c.unit_id.in_(unit_ids), content.c.format.in_(tuple(formats)))
        .order_by(
            content.c.unit_id, sa.func.coalesce(variant.c.preferred, False).desc(), content.c.id
        )
    )
    found: dict[str, ExportSource] = {}
    for row in rows:
        if row.unit_id in found:
            continue
        # a copy the integrity check found damaged is not what is exported (EF-BIB-012)
        copies = [c for c in library_copies(connection, row.unit_id) if c.independent]
        found[row.unit_id] = ExportSource(
            row.unit_id,
            row.content_id,
            row.content_hash,
            row.original_sha256,
            bool(row.watermarked),
            row.page_count,
            copies[0].local if copies else None,
            row.variant_id,
            row.content_hash_by,
            row.derived_hash,
            row.derived_by,
        )
    return found


def record_destination_copy(
    connection: Connection,
    *,
    unit_id: str,
    export_item_id: str,
    content_hash: str,
    file_hash: str,
    physical_root_id: str,
    rel_path: str,
    size: int,
    mtime_ns: int,
    ino: int,
    dev: int | None,
    verified_at: datetime,
) -> str:
    """The rendered file in a destination tree. Never counted as independent: Kavita may rewrite
    it, and it is not the original (`independent_copies` counts the others). A file put back after
    it vanished replaces the stale row of the same item (EF-EXP-018)."""
    forget_destination_copies(connection, export_item_id)
    return record_replica(
        connection,
        unit_id=unit_id,
        export_item_id=export_item_id,
        location="destination",
        content_hash=content_hash,
        file_hash=file_hash,
        independent=False,
        mutable_by_third_party=True,
        physical_root_id=physical_root_id,
        rel_path=rel_path,
        dev=dev,
        ino=ino,
        size=size,
        mtime_ns=mtime_ns,
        verified_at=verified_at,
    )


def upgrade_source(connection: Connection, unit_id: str, content_id: str) -> ExportSource | None:
    """The archive of one named `content` of the Unit and its library copy, for a same-path
    upgrade (EF-EXP-016): None when the content is not this Unit's CBZ or no library copy of it
    is known."""
    content = storage.content
    row = connection.execute(
        sa.select(content).where(
            content.c.id == content_id, content.c.unit_id == unit_id, content.c.format == "cbz"
        )
    ).one_or_none()
    if row is None:
        return None
    copies = [  # a copy the integrity check found damaged is never the source of a rewrite
        c
        for c in library_copies(connection, unit_id)
        if c.content_hash == row.content_hash and c.independent
    ]
    if not copies:
        return None
    return ExportSource(
        unit_id, row.id, row.content_hash, row.original_sha256, bool(row.watermarked),
        row.page_count, copies[0].local, row.variant_id, row.content_hash_by, row.derived_hash,
        row.derived_by,
    )  # fmt: skip


@dataclass(frozen=True)
class BinnedFile:
    trash_item_id: str
    export_item_id: str
    physical_root_id: str
    path: str  # relative to the root's bin: `<entry>/<path of the file in the root>`


def record_binned_export_file(
    connection: Connection,
    *,
    export_item_id: str,
    physical_root_id: str,
    path: str,
    size: int | None,
    now: datetime,
    retention: timedelta = BIN_RETENTION,
) -> str:
    """An exported file moved into the bin of its root (EF-EXP-016): one `trash_item`, so that a
    purge reaches it (EF-BIB-052) and a restore finds it. Kept `retention` (ENF-OPS-009)."""
    trash_id = new_ulid()
    connection.execute(
        sa.insert(storage.trash_item).values(
            id=trash_id, kind="export_file", ref=export_item_id, path=path,
            physical_root_id=physical_root_id, size=size, deleted_at=now,
            purge_after=now + retention,
        )
    )  # fmt: skip
    return trash_id


def forget_binned_export_files(connection: Connection, entry: str) -> int:
    """The bin entry `entry` (its name under the recycle directory) was removed: its exported
    files leave the bin index too, so that no restore points at a file the sweep took."""
    trash = storage.trash_item
    result = connection.execute(
        sa.delete(trash).where(
            trash.c.kind == "export_file",
            sa.or_(trash.c.path == entry, trash.c.path.startswith(f"{entry}/", autoescape=True)),
        )
    )
    return int(result.rowcount or 0)


def binned_export_file(connection: Connection, trash_item_id: str) -> BinnedFile | None:
    trash = storage.trash_item
    row = connection.execute(
        sa.select(trash).where(trash.c.id == trash_item_id, trash.c.kind == "export_file")
    ).one_or_none()
    if row is None or row.physical_root_id is None:
        return None
    return BinnedFile(row.id, row.ref, row.physical_root_id, row.path)
