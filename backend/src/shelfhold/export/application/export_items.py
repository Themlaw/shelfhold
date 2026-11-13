"""Export items and exported series in the database (EF-BIB-044, EF-EXP-062)."""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from shelfhold.export.domain.export_item import CoverageError, check_coverage
from shelfhold.export.domain.planner import SeriesRef
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.infrastructure.schema._base import metadata as _METADATA

__all__ = [
    "CoverageError",
    "FrozenSeriesError",
    "SeriesCollisionError",
    "SeriesMove",
    "SeriesReassignmentPlan",
    "SeriesReassignmentRefusedError",
    "add_coverage",
    "apply_series_reassignment",
    "create_export_item",
    "ensure_export_series",
    "plan_series_reassignment",
    "reassign_edition_series",
]


class FrozenSeriesError(Exception):
    """An adopted series keeps its name, its folder and its Editions (EF-EXP-052)."""


class SeriesCollisionError(Exception):
    """Another series of the remote library took this normalized name since the plan."""

    code = "export.series_name_collision"


def create_export_item(
    connection: Connection,
    *,
    edition_id: str,
    unit_id: str,
    kind: str,
    frozen_attrs: dict[str, object],
    content_id: str | None = None,
    item_id: str | None = None,
) -> str:
    """The item and its coverage, together: an item without its Unit would break the invariant."""
    check_coverage(kind, [unit_id])
    item_id = item_id or new_ulid()
    connection.execute(
        sa.insert(export.export_item).values(
            id=item_id, edition_id=edition_id, kind=kind, frozen_attrs=frozen_attrs,
            content_id=content_id,
        )
    )  # fmt: skip
    add_coverage(connection, item_id, unit_id)
    return item_id


def add_coverage(connection: Connection, item_id: str, unit_id: str) -> None:
    """Cover one more Unit; refused for a file Shelfhold wrote, which covers exactly one."""
    kind = connection.execute(
        sa.select(export.export_item.c.kind).where(export.export_item.c.id == item_id)
    ).scalar_one()
    table = export.unit_coverage
    covered = list(
        connection.execute(
            sa.select(table.c.unit_id).where(
                table.c.carrier_type == "export_item", table.c.carrier_id == item_id
            )
        ).scalars()
    )
    if covered:
        check_coverage(kind, [*covered, unit_id])
    connection.execute(
        sa.insert(table).values(
            carrier_type="export_item", carrier_id=item_id, unit_id=unit_id,
            position=len(covered), extent="full", origin="manual",
        )
    )  # fmt: skip


def ensure_export_series(
    connection: Connection,
    *,
    shelf_destination_id: str,
    remote_library_id: str,
    ref: SeriesRef,
    now: datetime,
    assigned_by: str = "first_export",
) -> str:
    """The series of an Edition in a remote library; created once, never recomputed.

    An existing link is returned as it is, whatever the Work is called today: a merge, a split or
    a rename of the Work changes nothing here (I26).
    """
    link = export.edition_export_series
    existing = connection.execute(
        sa.select(link.c.export_series_id).where(
            link.c.edition_id == ref.edition_id, link.c.remote_library_id == remote_library_id
        )
    ).scalar()
    if existing is not None:
        return str(existing)
    series_id = ref.export_series_id or _series_of_same_group(connection, remote_library_id, ref)
    if series_id is not None:  # another album of the Series made it: this Edition joins it
        connection.execute(
            sa.insert(link).values(
                edition_id=ref.edition_id, remote_library_id=remote_library_id,
                export_series_id=series_id, assigned_at=now, assigned_by=assigned_by,
            )
        )  # fmt: skip
        return series_id
    series_id = new_ulid()
    try:
        with connection.begin_nested():
            connection.execute(
                sa.insert(export.export_series).values(
                    id=series_id, shelf_destination_id=shelf_destination_id,
                    remote_library_id=remote_library_id, proposal_key=dict(ref.proposal_key or {}),
                    normalized_name=ref.normalized_name, name_raw=ref.name_raw,
                )
            )  # fmt: skip
    except IntegrityError:
        raise SeriesCollisionError(ref.name_raw) from None
    connection.execute(
        sa.insert(link).values(
            edition_id=ref.edition_id, remote_library_id=remote_library_id,
            export_series_id=series_id, assigned_at=now, assigned_by=assigned_by,
        )
    )  # fmt: skip
    return series_id


def _series_of_same_group(
    connection: Connection, remote_library_id: str, ref: SeriesRef
) -> str | None:
    """The exported series of this library already made for the same catalog Series and name."""
    group = (ref.proposal_key or {}).get("series")
    if not group:
        return None
    series = export.export_series
    row = connection.execute(
        sa.select(series.c.id, series.c.proposal_key).where(
            series.c.remote_library_id == remote_library_id,
            series.c.normalized_name == ref.normalized_name,
        )
    ).first()
    if row is None or (row.proposal_key or {}).get("series") != group:
        return None
    return str(row.id)


def reassign_edition_series(
    connection: Connection,
    *,
    edition_id: str,
    remote_library_id: str,
    shelf_destination_id: str,
    ref: SeriesRef,
    now: datetime,
) -> str:
    """Move the link of an Edition to another exported series: the step of an explicit plan
    (EF-EXP-062), taken after a Series was attached late or Works were merged (I26).

    Only `edition_export_series` changes: no file, no item, no record, no name of an existing
    series is touched, and an adopted series, whose name and folder are frozen, is never left.
    """
    link, series = export.edition_export_series, export.export_series
    with connection.begin_nested():
        current = connection.execute(
            sa.select(link.c.export_series_id, series.c.frozen_at)
            .join(series, series.c.id == link.c.export_series_id)
            .where(link.c.edition_id == edition_id, link.c.remote_library_id == remote_library_id)
        ).first()
        if current is not None:
            if current.frozen_at is not None:
                raise FrozenSeriesError(str(current.export_series_id))
            connection.execute(
                sa.delete(link).where(
                    link.c.edition_id == edition_id, link.c.remote_library_id == remote_library_id
                )
            )
        return ensure_export_series(
            connection, shelf_destination_id=shelf_destination_id,
            remote_library_id=remote_library_id, ref=ref, now=now, assigned_by="plan",
        )  # fmt: skip


class SeriesReassignmentRefusedError(Exception):
    """The plan is stale (the data moved since it was shown) or has blockers: nothing changed."""

    code = "export.series_reassignment_refused"

    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("series reassignment refused: " + ", ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class SeriesMove:
    edition_id: str
    remote_library_id: str
    from_series_id: str
    to_series_id: str


@dataclass(frozen=True)
class SeriesReassignmentPlan:
    """After Works were merged (or a Series attached late), the Editions of one Work that sit in
    several exported series of one remote library, and the series they would all share (I26)."""

    work_id: str
    moves: tuple[SeriesMove, ...]
    blockers: tuple[str, ...]
    path_changes: int  # moves out of a series whose folder was written: the next path differs
    emptied: tuple[str, ...]  # the series that no Edition would be linked to: they go, no orphan
    fingerprint: str

    @property
    def mergeable(self) -> bool:
        return not self.blockers


def _referencing(connection: Connection, series_id: str) -> list[str]:
    """The tables, other than the Edition links, that hold the exported series."""
    found: list[str] = []
    for table in _METADATA.sorted_tables:
        if table.name in ("edition_export_series", "export_series"):
            continue
        for fk in table.foreign_keys:
            if fk.target_fullname == "export_series.id":
                hit = connection.execute(
                    sa.select(sa.literal(1))
                    .select_from(table)
                    .where(fk.parent == series_id)
                    .limit(1)
                ).first()
                if hit is not None:
                    found.append(table.name)
    return found


def plan_series_reassignment(connection: Connection, work_id: str) -> SeriesReassignmentPlan:
    """The explicit plan that moves the link of the Editions of `work_id` onto one exported series
    per remote library. Writes nothing. The series kept is the one most Editions already share
    (the oldest on a tie): the others are left, and an emptied series is removed with its link
    rows (never orphaned, never twinned: the unique name of the library stays one row)."""
    link, series, edition = export.edition_export_series, export.export_series, catalog.edition
    rows = connection.execute(
        sa.select(link.c.edition_id, link.c.remote_library_id, link.c.export_series_id,
                  link.c.assigned_at)
        .join(edition, edition.c.id == link.c.edition_id)
        .where(edition.c.work_id == work_id)
        .order_by(link.c.remote_library_id, link.c.edition_id)
    ).all()  # fmt: skip
    moves: list[SeriesMove] = []
    blockers: set[str] = set()
    path_changes = 0
    emptied: set[str] = set()
    by_library: dict[str, list[Any]] = {}
    for r in rows:
        by_library.setdefault(r.remote_library_id, []).append(r)
    for library, group in sorted(by_library.items()):
        kinds = {r.export_series_id for r in group}
        if len(kinds) < 2:
            continue
        count = {s: sum(1 for r in group if r.export_series_id == s) for s in kinds}
        oldest = {s: min(r.assigned_at for r in group if r.export_series_id == s) for s in kinds}
        target = sorted(kinds, key=lambda s: (-count[s], oldest[s], s))[0]
        for r in group:
            if r.export_series_id == target:
                continue
            moves.append(SeriesMove(r.edition_id, library, r.export_series_id, target))
            left = connection.execute(
                sa.select(series.c.frozen_at, series.c.series_dir).where(
                    series.c.id == r.export_series_id
                )
            ).one()
            if left.frozen_at is not None:
                blockers.add("frozen_series")  # an adopted series is never left (EF-EXP-052)
            if left.series_dir is not None:
                path_changes += 1
        for s in kinds - {target}:
            total = connection.execute(
                sa.select(sa.func.count()).select_from(link).where(link.c.export_series_id == s)
            ).scalar_one()
            if total == count[s]:  # every link of it is moving: it would be left empty
                emptied.add(s)
                blockers.update(f"dependent:{t}" for t in _referencing(connection, s))
    body = {
        "work": work_id, "moves": [(m.edition_id, m.from_series_id, m.to_series_id) for m in moves],
        "blockers": sorted(blockers), "emptied": sorted(emptied), "paths": path_changes,
    }  # fmt: skip
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return SeriesReassignmentPlan(
        work_id, tuple(moves), tuple(sorted(blockers)), path_changes, tuple(sorted(emptied)), digest
    )


def apply_series_reassignment(
    connection: Connection, work_id: str, *, confirm: str, now: datetime
) -> SeriesReassignmentPlan:
    """Apply the plan whose fingerprint the admin read, recomputed here: a plan that moved, or one
    with blockers, changes nothing. Only the links move (and the emptied series go)."""
    plan = plan_series_reassignment(connection, work_id)
    if not hmac.compare_digest(plan.fingerprint.encode(), confirm.strip().encode()):
        raise SeriesReassignmentRefusedError(("plan.stale",))
    if not plan.mergeable:
        raise SeriesReassignmentRefusedError(plan.blockers)
    series = export.export_series
    with connection.begin_nested():
        for move in plan.moves:
            target = connection.execute(
                sa.select(series).where(series.c.id == move.to_series_id)
            ).one()
            reassign_edition_series(
                connection, edition_id=move.edition_id, remote_library_id=move.remote_library_id,
                shelf_destination_id=target.shelf_destination_id,
                ref=SeriesRef(
                    move.edition_id, target.name_raw, target.normalized_name,
                    export_series_id=move.to_series_id,
                ),
                now=now,
            )  # fmt: skip
        for series_id in plan.emptied:
            connection.execute(sa.delete(series).where(series.c.id == series_id))
    return plan
