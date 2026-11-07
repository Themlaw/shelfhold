"""Export items and exported series in the database (EF-BIB-044, EF-EXP-062)."""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from shelfhold.export.domain.export_item import CoverageError, check_coverage
from shelfhold.export.domain.planner import SeriesRef
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export

__all__ = [
    "CoverageError",
    "FrozenSeriesError",
    "SeriesCollisionError",
    "add_coverage",
    "create_export_item",
    "ensure_export_series",
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
