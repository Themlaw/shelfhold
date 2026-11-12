"""The bin index of what a deletion moved (EF-BIB-021): one `trash_item` per file, with what a
restore needs. The files are `<root>/.shelfhold/recycle/<entry>/<path in the root>`; the row says
which root, which entry, and keeps the registry row and the export record as they were."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from shelfhold.library.domain.deletion import BIN_DAYS

__all__ = [
    "BinnedUnitFile",
    "binned_for_unit",
    "forget_bin_rows",
    "record_binned_file",
]

_trash = storage.trash_item


@dataclass(frozen=True)
class BinnedUnitFile:
    trash_id: str
    deletion_id: str
    level: str
    root_id: str
    path: str  # `<entry>/<rel path in the root>`, relative to the recycle directory
    snapshot: dict[str, Any]


def record_binned_file(
    connection: Connection,
    *,
    kind: str,
    ref: str,
    level: str,
    deletion_id: str,
    root_id: str,
    path: str,
    size: int | None,
    snapshot: dict[str, Any],
    now: datetime,
) -> str:
    """Written in the transaction that holds the intent, before the file moves: a crash leaves the
    intent, whose replay moves the file into the entry this row names."""
    trash_id = new_ulid()
    connection.execute(
        sa.insert(_trash).values(
            id=trash_id, kind=kind, ref=ref, path=path, physical_root_id=root_id, size=size,
            deleted_at=now, purge_after=now + timedelta(days=BIN_DAYS), level=level,
            deletion_id=deletion_id, snapshot=snapshot,
        )
    )  # fmt: skip
    return trash_id


def binned_for_unit(connection: Connection, unit_id: str) -> list[BinnedUnitFile]:
    """What a restore of the Unit would put back: the entries of every deletion that recorded one
    for it (never the entries of before the snapshots existed)."""
    rows = connection.execute(
        sa.select(_trash)
        .where(_trash.c.snapshot.is_not(None), _trash.c.deletion_id.is_not(None))
        .order_by(_trash.c.id)
    )
    return [
        BinnedUnitFile(r.id, r.deletion_id, r.level, r.physical_root_id, r.path, dict(r.snapshot))
        for r in rows
        if isinstance(r.snapshot, dict) and r.snapshot.get("unit_id") == unit_id
    ]


def forget_bin_rows(connection: Connection, trash_ids: list[str]) -> None:
    if trash_ids:
        connection.execute(sa.delete(_trash).where(_trash.c.id.in_(trash_ids)))
