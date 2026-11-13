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
from shelfhold.export.domain.naming import normalize_series_name
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
    """One Edition's link leaving a series. `to_series_id` is the series it joins; `None` with a
    `new_name` is a split: a new series, named for the Work, that the Edition (with the others of
    the same group) starts (« Séparer », EF-EXP-062)."""

    edition_id: str
    remote_library_id: str
    from_series_id: str
    to_series_id: str | None
    new_name: str | None = None


@dataclass(frozen=True)
class SeriesReassignmentPlan:
    """After Works were merged or separated (or a Series attached late), the Editions of one Work
    that sit in several exported series of one remote library, or in a series that belongs to
    another Work, and where they would go (I26)."""

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


def _owner_of(connection: Connection, series_id: str, work_ids: set[str]) -> str | None:
    """The Work a shared series belongs to: the one whose title is the series' name, else the one
    that was exported into it first."""
    series, link, edition, work = (
        export.export_series, export.edition_export_series, catalog.edition, catalog.work,
    )  # fmt: skip
    name = connection.execute(
        sa.select(series.c.normalized_name).where(series.c.id == series_id)
    ).scalar()
    titled = [
        str(w.id) for w in connection.execute(
            sa.select(work.c.id, work.c.title).where(work.c.id.in_(work_ids))
        )
        if normalize_series_name(w.title) == name
    ]  # fmt: skip
    if len(titled) == 1:
        return titled[0]
    first = connection.execute(
        sa.select(edition.c.work_id)
        .join(link, link.c.edition_id == edition.c.id)
        .where(link.c.export_series_id == series_id)
        .order_by(link.c.assigned_at, link.c.edition_id)
        .limit(1)
    ).scalar()
    return None if first is None else str(first)


def _same_catalog_series(connection: Connection, work_id: str, others: set[str]) -> bool:
    member = catalog.series_member

    def of(work: str) -> set[str]:
        return {
            str(r) for r in connection.execute(
                sa.select(member.c.series_id).where(member.c.work_id == work)
            ).scalars()
        }  # fmt: skip

    mine = of(work_id)
    return bool(mine) and all(mine & of(o) for o in others)


def _plan_splits(
    connection: Connection, work_id: str, by_library: dict[str, list[Any]],
    moves: list[SeriesMove], blockers: set[str], emptied: set[str],
) -> int:  # fmt: skip
    """The Editions of `work_id` that sit in a series that belongs to another Work leave it for a
    series of their own, named for their Work (what « Séparer » leaves behind)."""
    link, edition, series = export.edition_export_series, catalog.edition, export.export_series
    title = connection.execute(
        sa.select(catalog.work.c.title).where(catalog.work.c.id == work_id)
    ).scalar()
    if title is None:
        return 0
    paths = 0
    taken = {m.edition_id for m in moves}
    for library, group in sorted(by_library.items()):
        for series_id in sorted({r.export_series_id for r in group}):
            sharers = {
                str(w) for w in connection.execute(
                    sa.select(edition.c.work_id)
                    .join(link, link.c.edition_id == edition.c.id)
                    .where(link.c.export_series_id == series_id)
                ).scalars()
            }  # fmt: skip
            if sharers == {work_id} or _owner_of(connection, series_id, sharers) == work_id:
                continue  # it is this Work's series, or nobody else is in it
            if _same_catalog_series(connection, work_id, sharers - {work_id}):
                continue  # albums of one Series share its exported series on purpose
            mine = [r for r in group if r.export_series_id == series_id
                    and r.edition_id not in taken]  # fmt: skip
            if not mine:
                continue
            left = connection.execute(
                sa.select(series.c.frozen_at, series.c.series_dir).where(series.c.id == series_id)
            ).one()
            if left.frozen_at is not None:
                blockers.add("frozen_series")
            if left.series_dir is not None:
                paths += len(mine)
            name = normalize_series_name(str(title))
            if connection.execute(
                sa.select(series.c.id).where(
                    series.c.remote_library_id == library, series.c.normalized_name == name
                )
            ).first():
                blockers.add("name_collision")  # a series of this name is already there
            moves.extend(SeriesMove(r.edition_id, library, series_id, None, str(title))
                         for r in mine)  # fmt: skip
    return paths


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
    path_changes += _plan_splits(connection, work_id, by_library, moves, blockers, emptied)
    body = {
        "work": work_id,
        "moves": [(m.edition_id, m.from_series_id, m.to_series_id, m.new_name) for m in moves],
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
            if move.to_series_id is None:
                continue
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
        _apply_splits(connection, plan, now)
        for series_id in plan.emptied:
            connection.execute(sa.delete(series).where(series.c.id == series_id))
    return plan


def _apply_splits(connection: Connection, plan: SeriesReassignmentPlan, now: datetime) -> None:
    """The Editions of a split leave their series for one new series named for the Work: the
    first creates it, the others join it. Only links and the new series row are written."""
    series = export.export_series
    created: dict[tuple[str, str], str] = {}
    for move in plan.moves:
        if move.to_series_id is not None or move.new_name is None:
            continue
        source = connection.execute(
            sa.select(series.c.shelf_destination_id).where(series.c.id == move.from_series_id)
        ).scalar_one()
        made = created.get((move.remote_library_id, move.from_series_id))
        created[(move.remote_library_id, move.from_series_id)] = reassign_edition_series(
            connection, edition_id=move.edition_id, remote_library_id=move.remote_library_id,
            shelf_destination_id=str(source),
            ref=SeriesRef(
                move.edition_id, move.new_name, normalize_series_name(move.new_name),
                export_series_id=made,
            ),
            now=now,
        )  # fmt: skip
