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
    "BinEntry",
    "BinnedUnitFile",
    "bin_entry",
    "binned_for_unit",
    "forget_bin_rows",
    "latest_deletion",
    "record_binned_file",
    "units_binned_from_store",
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


def _unit_of(snapshot_column: Any) -> Any:
    return snapshot_column["unit_id"].as_string()


def latest_deletion(connection: Connection, unit_id: str) -> str | None:
    """The most recent deletion that binned a file of this Unit (ULIDs sort by time)."""
    return connection.execute(
        sa.select(_trash.c.deletion_id)
        .where(
            _trash.c.deletion_id.is_not(None),
            _unit_of(_trash.c.snapshot) == unit_id,
        )
        .order_by(_trash.c.id.desc())
        .limit(1)
    ).scalar()


def binned_for_unit(
    connection: Connection, unit_id: str, *, deletion_id: str | None = None
) -> list[BinnedUnitFile]:
    """What a restore of ONE deletion of the Unit would put back: the entries that deletion
    recorded for it (never the entries of before the snapshots existed). Without a deletion
    named, the most recent one. Filtered by the database, not here."""
    chosen = deletion_id if deletion_id is not None else latest_deletion(connection, unit_id)
    if chosen is None:
        return []
    rows = connection.execute(
        sa.select(_trash)
        .where(
            _trash.c.snapshot.is_not(None),
            _trash.c.deletion_id == chosen,
            _unit_of(_trash.c.snapshot) == unit_id,
        )
        .order_by(_trash.c.id)
    )
    return [
        BinnedUnitFile(r.id, r.deletion_id, r.level, r.physical_root_id, r.path, dict(r.snapshot))
        for r in rows
    ]


@dataclass(frozen=True)
class BinEntry:
    """What the index says of one bin entry: the Units whose files it holds, and the term the
    deletion gave it (the last of its files)."""

    units: frozenset[str]
    purge_after: datetime | None


def bin_entry(connection: Connection, entry: str) -> BinEntry:
    rows = connection.execute(
        sa.select(_trash.c.snapshot, _trash.c.purge_after).where(_trash.c.path.like(f"{entry}/%"))
    ).all()
    units = frozenset(
        str(r.snapshot["unit_id"])
        for r in rows
        if isinstance(r.snapshot, dict) and r.snapshot.get("unit_id")
    )
    # the term of a deletion (it wrote a snapshot); a same-path upgrade's old version has its own
    terms = [
        r.purge_after for r in rows if r.purge_after is not None and isinstance(r.snapshot, dict)
    ]
    return BinEntry(units, max(terms) if terms else None)


def units_binned_from_store(connection: Connection) -> frozenset[str]:
    """The Units whose /store copy sits in a bin: `store/INDEX.jsonl` must not cite them."""
    rows = connection.execute(
        sa.select(_trash.c.snapshot).where(_trash.c.snapshot.is_not(None))
    ).scalars()
    return frozenset(
        str(s["unit_id"])
        for s in rows
        if isinstance(s, dict) and s.get("place") == "store" and s.get("unit_id")
    )


def forget_bin_rows(connection: Connection, trash_ids: list[str]) -> None:
    if trash_ids:
        connection.execute(sa.delete(_trash).where(_trash.c.id.in_(trash_ids)))
