"""What "Mettre à l'abri" reads from the database (EF-BIB-038, EF-BIB-042, EF-SRC-009).

Two lists and one marker, nothing written to a disk:

- `owed_candidates`: the Units whose copy in /store is owed because their source is gone or has
  failed on content (EF-BIB-031 candidates, L15), still in passthrough, oldest first. A binding
  switched off for another reason (a source not loaded, a pause) owes nothing: only the content
  errors count (EF-BIB-042 (2)), so an outage never makes a mass copy (R34).
- `upstream_candidates`: the downloads Shelfhold fetched and may be asked to let go of, on a shelf
  that asked for it. Only a first sieve (the shelf and the ticket): the verdict is the rule's.
- the backlog row (`unit.safeguard_backlog`), written by the cycle and read by `status`.

`SafeguardInventory` is the one reader of the tables it names for this purpose.
"""

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.schema import acquisition, catalog, export, storage
from shelfhold.library.adapters.units import content_error_candidates
from shelfhold.library.domain.store_budget import Candidate
from shelfhold.library.replica import Disk, linked_now

__all__ = [
    "PurgeCandidate",
    "SafeguardInventory",
    "backlog_row",
    "content_hash_of",
    "gone_units",
    "owed_unit_ids",
    "record_backlog",
]

RECORD_VERIFIED = ("scanned", "resolved")  # Kavita was seen to hold the file (EF-INT-025)


@dataclass(frozen=True)
class PurgeCandidate:
    request_id: str
    unit_id: str
    content_id: str
    binding_id: str
    ticket: str
    plugin_id: str
    source_key: str
    remote_unit_id: str
    remote_id: str


def gone_units(connection: Connection) -> set[str]:
    """Units the origin no longer lists: marked removed, or every Variant it had is removed."""
    unit, variant = catalog.unit, catalog.unit_variant
    removed = {
        r
        for r in connection.execute(
            sa.select(unit.c.id).where(unit.c.availability == "removed")
        ).scalars()
    }
    live = sa.select(variant.c.unit_id).where(variant.c.removed_at.is_(None)).distinct().subquery()
    every_variant_gone = connection.execute(
        sa.select(variant.c.unit_id)
        .where(variant.c.unit_id.not_in(sa.select(live.c.unit_id)))
        .distinct()
    ).scalars()
    return removed | set(every_variant_gone)


def owed_unit_ids(connection: Connection) -> set[str]:
    """Candidates of the content-error count (L15) and Units whose origin is gone."""
    candidates = {c.unit_id for c in content_error_candidates(connection)}
    return candidates | gone_units(connection)


def backlog_row(connection: Connection) -> tuple[int, int] | None:
    """`(units, bytes)` left at the last cycle, None when it never ran or the queue is empty."""
    row = connection.execute(
        sa.select(storage.safeguard_backlog.c.units, storage.safeguard_backlog.c.bytes)
    ).first()
    if row is None or row.units == 0:
        return None
    return int(row.units), int(row.bytes)


def record_backlog(connection: Connection, *, units: int, size: int, now: datetime) -> None:
    """Say what the cycle left behind; zero empties the queue (and forgets since when)."""
    table = storage.safeguard_backlog
    since = connection.execute(sa.select(table.c.since)).scalar()
    insert = sqlite_insert(table).values(
        name="store", units=units, bytes=size, since=now if units else None, updated_at=now
    )
    connection.execute(
        insert.on_conflict_do_update(
            index_elements=["name"],
            set_={
                "units": units,
                "bytes": size,
                "since": (since or now) if units else None,
                "updated_at": now,
            },
        )
    )


class SafeguardInventory:
    def __init__(self, engine: Engine, fsops: Disk | None = None) -> None:
        self._engine = engine
        self._fsops = fsops  # given: a kept copy is a link or not as the disk says now

    def owed_candidates(self) -> list[Candidate]:
        content = storage.content
        with self._engine.connect() as connection:
            owed = owed_unit_ids(connection)
            if not owed:
                return []
            rows = connection.execute(
                sa.select(content.c.unit_id, content.c.size, content.c.id, content.c.retention)
                .where(
                    content.c.retention.in_(
                        ("passthrough", "keep") if self._fsops is not None else ("passthrough",)
                    ),
                    content.c.unit_id.in_(owed),
                    sa.exists().where(catalog.unit_variant.c.unit_id == content.c.unit_id),
                )
                .order_by(content.c.id)
            ).all()
            kept = {r.unit_id for r in rows if r.retention == "keep"}
            linked = (
                {u for u in kept if linked_now(connection, self._fsops, u)}
                if self._fsops
                else set()
            )
        found: dict[str, Candidate] = {}
        for r in rows:
            if r.retention == "keep" and r.unit_id not in linked:
                continue
            found.setdefault(r.unit_id, Candidate(r.unit_id, int(r.size)))
        return list(found.values())

    def upstream_candidates(self, *, request_id: str | None = None) -> list[PurgeCandidate]:
        """Downloads fetched and not yet let go of, whose content was exported to a shelf that
        asked for the upstream copy to go. Oldest first. `request_id`: that one request, whatever
        the option says now (an erasure already begun is settled, not decided again)."""
        r, v, b, si = (
            acquisition.download_request, catalog.unit_variant, acquisition.source_binding,
            acquisition.source_item,
        )  # fmt: skip
        c, item, rec = storage.content, export.export_item, export.export_record
        dest, shelf = export.shelf_destination, export.shelf
        query = (
            sa.select(
                r.c.id, v.c.unit_id, c.c.id.label("content_id"), r.c.source_binding_id, r.c.ticket,
                si.c.plugin_id, si.c.source_key, v.c.remote_unit_id, si.c.remote_id,
            )
            .select_from(
                r.join(v, v.c.id == r.c.variant_id)
                .join(b, b.c.id == r.c.source_binding_id)
                .join(si, si.c.id == b.c.source_item_id)
                .join(c, c.c.variant_id == v.c.id)
            )
            .where(
                r.c.state == "fetched", r.c.target_kind == "unit", r.c.ticket.is_not(None),
                *(
                    [r.c.id == request_id]
                    if request_id is not None
                    else [
                        sa.exists().where(
                            item.c.content_id == c.c.id,
                            rec.c.export_item_id == item.c.id,
                            dest.c.id == rec.c.shelf_destination_id,
                            shelf.c.id == dest.c.shelf_id,
                            shelf.c.purge_upstream_after_export,
                        )
                    ]
                ),
            )
            .order_by(r.c.requested_at, r.c.id)
        )  # fmt: skip
        with self._engine.connect() as connection:
            return [PurgeCandidate(*tuple(row)) for row in connection.execute(query)]

    # -- what the rule needs about a Unit's exports ---------------------------------------------

    def export_facts(self, connection: Connection, content_id: str) -> tuple[bool, bool]:
        """`(every shelf that holds it asked for the upstream copy to go, every export of it is
        verified)`. One shelf that did not ask, or one record not seen in Kavita, is a no."""
        item, rec = export.export_item, export.export_record
        dest, shelf = export.shelf_destination, export.shelf
        rows = connection.execute(
            sa.select(rec.c.state, shelf.c.purge_upstream_after_export)
            .select_from(
                rec.join(item, item.c.id == rec.c.export_item_id)
                .join(dest, dest.c.id == rec.c.shelf_destination_id)
                .join(shelf, shelf.c.id == dest.c.shelf_id)
            )
            .where(item.c.content_id == content_id)
        ).all()
        if not rows:
            return False, False
        return (
            all(bool(r.purge_upstream_after_export) for r in rows),
            all(r.state in RECORD_VERIFIED for r in rows),
        )

    def source_alive(self, connection: Connection, unit_id: str) -> bool:
        """An active binding still lists the Unit (the origin could give it back)."""
        unit, variant, binding = catalog.unit, catalog.unit_variant, acquisition.source_binding
        if connection.execute(
            sa.select(unit.c.id).where(unit.c.id == unit_id, unit.c.availability == "removed")
        ).first():
            return False
        return (
            connection.execute(
                sa.select(variant.c.id)
                .join(binding, binding.c.id == variant.c.source_binding_id)
                .where(
                    variant.c.unit_id == unit_id, variant.c.removed_at.is_(None), binding.c.active
                )
                .limit(1)
            ).first()
            is not None
        )


def content_hash_of(connection: Connection, content_id: str) -> str | None:
    """The payload hash of one content: what a copy must have to be a copy of it (a Unit may hold
    several contents, one per Variant)."""
    return connection.execute(
        sa.select(storage.content.c.content_hash).where(storage.content.c.id == content_id)
    ).scalar_one_or_none()
