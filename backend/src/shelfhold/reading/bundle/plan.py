"""What a block is made of, read from the manifest, and where its parts begin (ADR-0053 §8).

Nothing here opens a file: the feed lists a shelf of series with this, and a file is opened only
when a version is made or served. A chapter of the block is an `export_item` of the Edition with a
live record that is a path in a tree destination; the Units it covers give its place in the series.
A `pull` destination has no file of its own (its record has no locator): an `export_item` it
carries that no tree destination holds is read from the kept copy of its Unit (`/library`, never
the original of `/store`), when that copy is a CBZ the integrity check has not marked damaged
(M5 L06, EF-EXP-058): a `pull` binding is served only from a readable copy, else not at all.

Parts are cut on boundaries that are *written* (`bundle_partition.first_unit_id`) when a part is
first made, and never move: a chapter added goes to the last part until the cap, then opens the
next; a chapter replaced at the start changes its own part and no other. A boundary whose Unit
has gone (the series was re-cut) ends the written prefix: what follows is planned again.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import bundle, catalog, export, storage
from shelfhold.library.public import library_copies
from shelfhold.reading.bundle import store
from shelfhold.reading.bundle.limits import BundleLimits
from shelfhold.reading.bundle.sources import clean_relative, root_of

LIVE: Final = ("written", "scanned", "resolved", "adopted")
# A chapter is the file of a Unit, of a volume or of a range's nameless volume (`bundle`,
# EF-BIB-044). The carrier of a part's aliases is of another kind (`opds_bundle`): no export plan
# ever reads it.
ITEM_KINDS: Final = ("unit", "volume", "bundle")
CARRIER_KIND: Final = "opds_bundle"


@dataclass(frozen=True)
class Candidate:
    item_id: str
    unit_id: str  # the first Unit it covers, in the order of the series
    number: str
    order: tuple[Any, ...]
    destination_id: str
    rel_path: str
    format: str | None
    size: int | None
    mtime_ns: int | None
    file_hash: str | None
    root: str | None

    @property
    def left_out(self) -> str | None:
        """A reason known from the manifest alone, without a file."""
        if (self.format or "cbz") != "cbz":
            return "format"
        if self.root is None or not clean_relative(self.rel_path):
            return "unsafe"
        return None


def load_candidates(connection: Connection, edition_id: str) -> list[Candidate]:
    item, coverage, unit, record = (
        export.export_item,
        export.unit_coverage,
        catalog.unit,
        export.export_record,
    )
    destination, target = export.shelf_destination, export.export_target
    rows = connection.execute(
        sa.select(
            item.c.id.label("item_id"),
            unit.c.id.label("unit_id"),
            unit.c.number_norm,
            unit.c.number_sort,
            unit.c.number_segment_ordinal,
            unit.c.source_order,
            record.c.shelf_destination_id,
            record.c.rel_path,
            record.c.format,
            record.c.size,
            record.c.mtime_ns,
            record.c.file_hash,
            destination.c.root_subpath,
            target.c.path_map,
        )
        .select_from(
            item.join(
                coverage,
                sa.and_(
                    coverage.c.carrier_type == "export_item", coverage.c.carrier_id == item.c.id
                ),
            )
            .join(unit, unit.c.id == coverage.c.unit_id)
            .join(
                record,
                sa.and_(
                    record.c.export_item_id == item.c.id,
                    record.c.locator_type == "path",
                    record.c.state.in_(LIVE),
                ),
            )
            .join(destination, destination.c.id == record.c.shelf_destination_id)
            .join(target, target.c.id == destination.c.target_id)
        )
        .where(
            item.c.edition_id == edition_id,
            item.c.kind.in_(ITEM_KINDS),
            destination.c.transport == "tree",
        )
    ).all()
    best: dict[str, Candidate] = {}
    for r in rows:
        order = (
            r.number_sort is None,
            r.number_sort or 0.0,
            r.number_segment_ordinal,
            r.source_order,
            r.unit_id,
        )
        found = Candidate(
            r.item_id,
            r.unit_id,
            r.number_norm,
            order,
            r.shelf_destination_id,
            r.rel_path or "",
            r.format,
            r.size,
            r.mtime_ns,
            r.file_hash,
            root_of(r.path_map, r.root_subpath),
        )
        held = best.get(r.item_id)
        if held is None or (found.order, found.destination_id) < (held.order, held.destination_id):
            best[r.item_id] = found  # the first Unit it covers, the lowest destination
    for found in _kept_copies(connection, edition_id, set(best)):
        best[found.item_id] = found
    return sorted(best.values(), key=lambda c: (c.order, c.item_id))


def _kept_copies(connection: Connection, edition_id: str, held: set[str]) -> list[Candidate]:
    """The chapters a `pull` destination carries and no tree destination holds, read from the
    kept copy of the one Unit they cover (EF-EXP-058).

    Nothing ties an `export_item` to what it points at, so each link is checked here: the Unit is
    of this Edition (the one the reader's scope was checked on), the item covers that Unit alone
    (a volume of several is no one chapter's file and is left out), the file is a CBZ replica of
    the very content the item names, not damaged, and its folder, name, size and modification time
    are the registry's: `open_source` opens it beneath that folder and `_open_one` refuses a file
    that is no longer what the registry recorded (a 410, nothing sent)."""
    item, coverage, unit, record = (
        export.export_item,
        export.unit_coverage,
        catalog.unit,
        export.export_record,
    )
    destination, content = export.shelf_destination, storage.content
    live_pull = (
        sa.select(sa.literal(1))
        .select_from(record.join(destination, destination.c.id == record.c.shelf_destination_id))
        .where(
            record.c.export_item_id == item.c.id,
            record.c.state.in_(LIVE),
            destination.c.transport == "pull",
        )
        .exists()
    )
    rows = connection.execute(
        sa.select(
            item.c.id.label("item_id"),
            item.c.content_id,
            unit.c.id.label("unit_id"),
            unit.c.edition_id,
            unit.c.number_norm,
            unit.c.number_sort,
            unit.c.number_segment_ordinal,
            unit.c.source_order,
        )
        .select_from(
            item.join(
                coverage,
                sa.and_(
                    coverage.c.carrier_type == "export_item", coverage.c.carrier_id == item.c.id
                ),
            ).join(unit, unit.c.id == coverage.c.unit_id)
        )
        .where(item.c.edition_id == edition_id, item.c.kind.in_(ITEM_KINDS), live_pull)
        .order_by(item.c.id, coverage.c.position)
    ).all()
    covered: dict[str, list[Any]] = {}
    for r in rows:
        if r.item_id not in held:
            covered.setdefault(r.item_id, []).append(r)
    found: list[Candidate] = []
    for item_id, units in covered.items():
        if len(units) != 1 or units[0].edition_id != edition_id:
            continue
        r = units[0]
        kept = connection.execute(
            sa.select(content.c.id, content.c.content_hash, content.c.format).where(
                content.c.unit_id == r.unit_id,
                *([content.c.id == r.content_id] if r.content_id is not None else []),
            )
        ).all()
        copy = next(
            (
                c
                for k in kept
                if k.format == "cbz"
                for c in library_copies(connection, r.unit_id)
                if not c.damaged
                and c.rel_path.lower().endswith(".cbz")
                and (k.content_hash is None or c.content_hash == k.content_hash)
            ),
            None,
        )
        if copy is None or not copy.root.startswith("/") or not clean_relative(copy.rel_path):
            continue
        order = (
            r.number_sort is None,
            r.number_sort or 0.0,
            r.number_segment_ordinal,
            r.source_order,
            r.unit_id,
        )
        found.append(
            Candidate(
                item_id,
                r.unit_id,
                r.number_norm,
                order,
                "",
                copy.rel_path,
                "cbz",
                copy.size,
                copy.mtime_ns,
                copy.file_hash,
                copy.root,
            )
        )
    return found


@dataclass(frozen=True)
class Part:
    index: int
    members: tuple[Candidate, ...]
    first_unit_id: str | None  # None for the first part: it opens the series

    @property
    def state_key(self) -> str:
        """A digest of the manifest rows of the part: equal while nothing it is made of changed,
        without opening a file."""
        text = "".join(f"{m.item_id}|{m.file_hash}|{m.size}\n" for m in self.members)
        return hashlib.sha256(f"{self.index}\n{text}".encode()).hexdigest()


def stored_boundaries(connection: Connection, edition_id: str) -> list[tuple[int, str | None]]:
    part = bundle.bundle_partition
    rows = connection.execute(
        sa.select(part.c.part_index, part.c.first_unit_id)
        .where(part.c.edition_id == edition_id)
        .order_by(part.c.part_index)
    )
    return [(r.part_index, r.first_unit_id) for r in rows]


def plan_parts(
    stored: list[tuple[int, str | None]],
    eligible: list[Candidate],
    limits: BundleLimits,
    slice_chapters: int | None = None,
) -> list[Part]:
    """Cut `eligible` (in the order of the series) into parts: the written boundaries first, then
    a greedy fill of the last part by the sizes the manifest gives. `slice_chapters` (L49b) only
    narrows how many chapters that fill puts in a part: a written boundary does not move."""
    if not eligible:
        return []
    position = {c.unit_id: i for i, c in enumerate(eligible)}
    starts = [0]
    for index, unit_id in stored:
        if index == 1:
            continue
        at = position.get(unit_id) if unit_id else None
        if at is None or at <= starts[-1] or index != len(starts) + 1:
            break  # a boundary that is gone ends the written prefix
        starts.append(at)
    members_of: list[list[Candidate]] = [
        eligible[a:b] for a, b in zip(starts, [*starts[1:], len(eligible)], strict=True)
    ]
    tail, filled = members_of.pop(), 0
    most = min(limits.members_per_part, slice_chapters or limits.members_per_part)
    members_of.append([])
    for candidate in tail:
        size = candidate.size or 0
        current = members_of[-1]
        if current and (filled + size > limits.part_bytes or len(current) >= most):
            members_of.append([])
            filled = 0
        members_of[-1].append(candidate)
        filled += size
    return [
        Part(i, tuple(members), members[0].unit_id if i > 1 else None)
        for i, members in enumerate(members_of, start=1)
    ]


def purge_stale_partitions(
    connection: Connection, edition_id: str, stale: list[int], carriers: list[str], now: datetime
) -> None:
    """Database rows only. The versions made on a boundary that went are taken out of service (they
    answer 410, their rows and numbers stay), then the boundary and its carrier go; the aliases
    go with the carrier."""
    table = bundle.bundle_partition
    for index in stale:
        store.purge_partition_versions(connection, edition_id, index, now)
    connection.execute(
        sa.delete(table).where(table.c.edition_id == edition_id, table.c.part_index.in_(stale))
    )
    connection.execute(sa.delete(export.export_item).where(export.export_item.c.id.in_(carriers)))


def ensure_partitions(
    connection: Connection, edition_id: str, parts: list[Part], now: datetime
) -> dict[int, str]:
    """Write the boundaries the plan has and the table lacks; drop those a gone Unit invalidated
    (their versions go with them). Returns `part_index -> export_item_id`. Call in a write
    transaction."""
    table = bundle.bundle_partition
    held = {
        r.part_index: (r.first_unit_id, r.export_item_id)
        for r in connection.execute(sa.select(table).where(table.c.edition_id == edition_id))
    }
    wanted = {p.index: p.first_unit_id for p in parts}
    stale = sorted(i for i, (unit, _) in held.items() if i not in wanted or wanted[i] != unit)
    if stale:
        purge_stale_partitions(connection, edition_id, stale, [held[i][1] for i in stale], now)
        for i in stale:
            held.pop(i)
    for part in parts:
        if part.index in held:
            continue
        item_id = new_ulid()
        connection.execute(
            sa.insert(export.export_item).values(
                id=item_id,
                edition_id=edition_id,
                kind=CARRIER_KIND,
                frozen_attrs={"part": part.index},
            )
        )
        connection.execute(
            sa.insert(table).values(
                edition_id=edition_id,
                part_index=part.index,
                first_unit_id=part.first_unit_id,
                export_item_id=item_id,
            )
        )
        held[part.index] = (part.first_unit_id, item_id)
    return {i: item_id for i, (_, item_id) in held.items()}
