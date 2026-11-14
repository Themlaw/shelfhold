"""The bin index of what a deletion moved (EF-BIB-021): one `trash_item` per file, with what a
restore needs. The files are `<root>/.shelfhold/recycle/<entry>/<path in the root>`; the row says
which root, which entry, and keeps the registry row and the export record as they were."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.library.domain.deletion import BIN_DAYS

__all__ = [
    "BinEntry",
    "BinnedUnitFile",
    "bin_entry",
    "binned_for_unit",
    "forget_bin_rows",
    "held_for_irreplaceable_units",
    "latest_deletion",
    "list_binned",
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


def held_for_irreplaceable_units(connection: Connection) -> dict[str, int]:
    """`unit -> bin entries` past their term that the sweep keeps because the Unit is
    irreplaceable now (EF-BIB-031): what `shelfhold status` shows, read from the index."""
    from shelfhold.library.adapters.units import safe_to_clean

    rows = connection.execute(
        sa.select(_trash.c.snapshot, _trash.c.path).where(
            _trash.c.snapshot.is_not(None), _trash.c.purge_after <= sa.func.current_timestamp()
        )
    ).all()
    entries: dict[str, set[str]] = {}
    for r in rows:
        if isinstance(r.snapshot, dict) and r.snapshot.get("unit_id"):
            entries.setdefault(str(r.snapshot["unit_id"]), set()).add(r.path.split("/", 1)[0])
    known = set(
        connection.execute(
            sa.select(catalog.unit.c.id).where(catalog.unit.c.id.in_(entries))
        ).scalars()
    )  # a Unit purged since has no row left to ask
    _allowed, kept = safe_to_clean(connection, sorted(known))
    return {unit: len(entries[unit]) for unit in kept}


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


def list_binned(connection: Connection) -> list[dict[str, Any]]:
    """What the bins hold, one line per deletion and Unit (the « Corbeille » screen): the term,
    the size, and whether the Unit is irreplaceable now (EF-BIB-031). Never a path or a snapshot:
    the screen needs neither, and a path names where a copy sat."""
    from shelfhold.library.adapters.units import irreplaceability

    rows = connection.execute(
        sa.select(
            _trash.c.deletion_id, _trash.c.snapshot, _trash.c.level, _trash.c.size,
            _trash.c.deleted_at, _trash.c.purge_after,
        )
        .where(_trash.c.snapshot.is_not(None))
        .order_by(_trash.c.id)
    ).all()  # fmt: skip
    lines: dict[tuple[str | None, str], dict[str, Any]] = {}
    for r in rows:
        unit = r.snapshot.get("unit_id") if isinstance(r.snapshot, dict) else None
        if not unit:
            continue
        line = lines.setdefault(
            (r.deletion_id, str(unit)),
            {"deletion_id": r.deletion_id, "unit_id": str(unit), "level": r.level, "files": 0,
             "bytes": 0, "deleted_at": r.deleted_at, "purge_after": r.purge_after},
        )  # fmt: skip
        line["files"] += 1
        line["bytes"] += r.size or 0
        line["purge_after"] = max(line["purge_after"], r.purge_after)
    known = set(
        connection.execute(
            sa.select(catalog.unit.c.id).where(catalog.unit.c.id.in_([u for _, u in lines]))
        ).scalars()
    )  # a Unit purged since has no row left to ask: it is not irreplaceable, it is gone
    found = []
    for line in lines.values():
        line["irreplaceable"] = (
            line["unit_id"] in known and irreplaceability(connection, line["unit_id"]).irreplaceable
        )
        line["deleted_at"] = line["deleted_at"].isoformat()
        line["purge_after"] = line["purge_after"].isoformat()
        found.append(line)
    return found
