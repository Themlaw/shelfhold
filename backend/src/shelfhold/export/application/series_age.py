"""The age Shelfhold wants for a Kavita series, read from the database alone (EF-EXP-040).

A file carries no age that Kavita could use for an EPUB, and the barrier of a family shelf is the
library, so the age of a series is pushed by the API once the Work is rated (`SeriesAges`). What
is wanted here is the class of the catalog: the most restrictive of the Work and its Editions,
counted only for the Editions that are in the library at this moment, that is, filed on the
shelf of the destination and admitted by the same last look the exporter takes before a write
(`barrier_holds`). An Edition filed elsewhere, or that the admission now refuses, is not an age to
push: its change of shelf is the business of the removal plan (L10), never of a metadata push.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.public import RatingService
from shelfhold.export.application.snapshot import barrier_holds, load_context
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.sdk.age import AgeRating, max_rating

__all__ = ["SeriesAgeTask", "due_ages"]

_WRITTEN = ("written", "scanned", "resolved")


@dataclass(frozen=True)
class SeriesAgeTask:
    export_series_id: str
    kavita_series_id: int | None  # the id cached by the arrival polling, if Kavita indexed it
    folder: str  # the remote folder of the series: how a reinstalled Kavita finds it again
    wanted: AgeRating


def due_ages(connection: Connection, destination_id: str) -> list[SeriesAgeTask]:
    """The series of the destination that have a class to push, in a stable order."""
    ctx = load_context(connection, destination_id)
    if ctx.remote_library_id is None:
        return []
    record, item, link = export.export_record, export.export_item, export.edition_export_series
    rows = connection.execute(
        sa.select(
            link.c.export_series_id,
            item.c.edition_id,
            record.c.state,
            record.c.remote_ref,
            record.c.rel_path,
        )
        .select_from(
            record.join(item, item.c.id == record.c.export_item_id).join(
                link,
                sa.and_(
                    link.c.edition_id == item.c.edition_id,
                    link.c.remote_library_id == ctx.remote_library_id,
                ),
            )
        )
        .where(
            record.c.shelf_destination_id == destination_id,
            record.c.locator_type == "path",
            record.c.state.in_(_WRITTEN),
            record.c.rel_path.is_not(None),
        )
        .order_by(record.c.rel_path)
    ).all()
    root = ctx.remote_folder(ctx.root_local) if ctx.root_local is not None else ""
    editions = {
        r.id: r
        for r in connection.execute(
            sa.select(catalog.edition.c.id, catalog.edition.c.work_id, catalog.edition.c.shelf_id)
            .where(catalog.edition.c.id.in_({r.edition_id for r in rows}))
        )
    }  # fmt: skip
    rating = RatingService()
    in_place: dict[str, bool] = {}
    by_series: dict[str, list[Any]] = {}
    for row in rows:
        by_series.setdefault(row.export_series_id, []).append(row)
    tasks: list[SeriesAgeTask] = []
    for series_id, series_rows in sorted(by_series.items()):
        classes: list[AgeRating] = []
        for edition_id in sorted({r.edition_id for r in series_rows}):
            if edition_id not in in_place:
                edition = editions[edition_id]
                in_place[edition_id] = edition.shelf_id == ctx.shelf_id and barrier_holds(
                    connection, destination_id, edition_id
                )
            if in_place[edition_id]:
                age = rating.effective_age_of(connection, editions[edition_id].work_id)
                if age is not None:
                    classes.append(age)
        wanted = max_rating(classes)
        if wanted is None:
            continue
        resolved = [
            int(r.remote_ref)
            for r in series_rows
            if r.state == "resolved" and str(r.remote_ref).isdigit()
        ]
        folder = posixpath.dirname(posixpath.join(root, series_rows[0].rel_path))
        tasks.append(SeriesAgeTask(series_id, resolved[0] if resolved else None, folder, wanted))
    return tasks
