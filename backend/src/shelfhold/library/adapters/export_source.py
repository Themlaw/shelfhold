"""What the export reads of the library: the received archive of a Unit and its hashes.

The export context never opens the storage tables or the registry of copies; it asks here
(EF-BIB-045), and records the file it wrote through `record_destination_copy`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.library.replica import library_copies, record_replica


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


def export_sources(connection: Connection, unit_ids: Sequence[str]) -> dict[str, ExportSource]:
    """The archive to export for each Unit: the preferred Variant's, else the first received."""
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
        .where(content.c.unit_id.in_(unit_ids), content.c.format == "cbz")
        .order_by(
            content.c.unit_id, sa.func.coalesce(variant.c.preferred, False).desc(), content.c.id
        )
    )
    found: dict[str, ExportSource] = {}
    for row in rows:
        if row.unit_id in found:
            continue
        copies = library_copies(connection, row.unit_id)
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
    it, and it is not the original (`independent_copies` counts the others)."""
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
