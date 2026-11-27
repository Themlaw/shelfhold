"""What a profile has read of an Edition, and which Unit opens the reading of a Work (API-1).

Read-only views of the `reading_progress` projection, for screens that show « Reprendre au
chapitre N » and for the list of Works. Visibility is the one filter of every surface
(`visible_editions_clause`), applied in the query itself: an Edition she may not see is neither an
answer nor a count, and the answer is the one of an Edition with no progress at all. The progress
is the profile's own: nothing here takes another profile's id.

* `edition_progress`: per Edition, the Unit she touched last (the main track), its place in the
  Unit (`percent`, when the position stored one), the device that wrote it, and when.
* `reading_units`: per Work, the Unit a reader opens — the one under way (the latest touched),
  else the first, by number, that has a file the reader can open; in an Edition she may see.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa

from shelfhold.catalog.public import visible_editions_clause
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.schema import catalog, reading, storage

__all__ = ["EditionProgress", "edition_progress", "reading_units"]

_CHUNK: Final = 400
_READABLE: Final = ("cbz", "epub")  # the formats of the integrated reader (`publication`)

# The view `reading_progress` (frozen columns, `docs/reference/reading-progress.md`).
_PROGRESS = sa.table(
    "reading_progress",
    sa.column("user_id"), sa.column("unit_id"), sa.column("state"),
    sa.column("latest_position_id"),
)  # fmt: skip


@dataclass(frozen=True)
class EditionProgress:
    edition_id: str
    unit_id: str  # the Unit she touched last
    number: str  # as the catalogue spells it: « chapitre N »
    unit_title: str | None
    state: str  # `in_progress` | `read`
    percent: float | None  # 0..1, in that Unit, when the position stored one
    device: str | None  # what the writer of the position called its device
    last_read_at: datetime  # the position's `client_ts`, bounded by the server's date
    read_count: int  # Units of the Edition she has read to the end


def _chunks(ids: Iterable[str]) -> Iterable[list[str]]:
    unique = sorted(set(ids))
    for start in range(0, len(unique), _CHUNK):
        yield unique[start : start + _CHUNK]


def _order(row: Any) -> tuple[bool, float, int, int, str]:
    """Reading order of the Units of an Edition (the order of the series bundle)."""
    return (
        row.number_sort is None,
        row.number_sort or 0.0,
        row.number_segment_ordinal,
        row.source_order,
        str(row.id),
    )


def edition_progress(
    connection: sa.Connection, user_id: str, edition_ids: Iterable[str]
) -> dict[str, EditionProgress]:
    """The progress of `user_id` on each of `edition_ids` she has started and may see."""
    scope = reader_scope(connection, user_id)
    if scope.kind == "none":
        return {}
    progress, unit, position = _PROGRESS, catalog.unit, reading.reading_position
    rows: list[Any] = []
    for chunk in _chunks(edition_ids):
        rows += connection.execute(
            sa.select(
                unit.c.id,
                unit.c.edition_id,
                unit.c.number_raw,
                unit.c.title,
                unit.c.number_sort,
                unit.c.number_segment_ordinal,
                unit.c.source_order,
                progress.c.state,
                position.c.percent,
                position.c.device_id,
                position.c.client_ts,
                position.c.server_ts,
            )
            .select_from(
                progress.join(unit, unit.c.id == progress.c.unit_id)
                .join(catalog.edition, catalog.edition.c.id == unit.c.edition_id)
                .join(position, position.c.id == progress.c.latest_position_id)
            )
            .where(
                progress.c.user_id == user_id,
                progress.c.state.in_(("in_progress", "read")),
                unit.c.edition_id.in_(chunk),
                unit.c.track == "main",
                visible_editions_clause(scope),
            )
        ).all()
    by_edition: dict[str, list[Any]] = {}
    for row in rows:
        by_edition.setdefault(str(row.edition_id), []).append(row)
    found: dict[str, EditionProgress] = {}
    for edition_id, group in by_edition.items():

        def touched(row: Any) -> datetime:
            return min(row.client_ts, row.server_ts)  # type: ignore[no-any-return]

        last = max(group, key=lambda r: (touched(r), _order(r)))
        found[edition_id] = EditionProgress(
            edition_id=edition_id, unit_id=str(last.id), number=str(last.number_raw),
            unit_title=last.title, state=str(last.state),
            percent=None if last.percent is None else float(last.percent),
            device=last.device_id, last_read_at=touched(last),
            read_count=sum(1 for r in group if r.state == "read"),
        )  # fmt: skip
    return found


def reading_units(
    connection: sa.Connection, user_id: str, work_ids: Iterable[str]
) -> dict[str, str]:
    """For each Work of `work_ids`, the Unit a reader opens: the one she has under way (the latest
    touched), else the first with a file the reader opens, in an Edition she may see. A Work with
    no such Unit is not in the answer."""
    scope = reader_scope(connection, user_id)
    if scope.kind == "none":
        return {}
    unit, edition, position, progress = (
        catalog.unit,
        catalog.edition,
        reading.reading_position,
        _PROGRESS,
    )
    openable = sa.exists().where(
        storage.content.c.unit_id == unit.c.id, storage.content.c.format.in_(_READABLE)
    )
    order = (unit.c.number_sort.is_(None), unit.c.number_sort, unit.c.number_segment_ordinal,
             unit.c.source_order, unit.c.id)  # fmt: skip
    found: dict[str, str] = {}
    for chunk in _chunks(work_ids):
        under_way = (
            sa.select(
                edition.c.work_id, unit.c.id,
                sa.func.row_number().over(
                    partition_by=edition.c.work_id,
                    order_by=(
                        sa.desc(sa.func.min(position.c.client_ts, position.c.server_ts)),
                        *order,
                    ),
                ).label("rn"),
            )
            .select_from(
                progress.join(unit, unit.c.id == progress.c.unit_id)
                .join(edition, edition.c.id == unit.c.edition_id)
                .join(position, position.c.id == progress.c.latest_position_id)
            )
            .where(
                progress.c.user_id == user_id, progress.c.state == "in_progress",
                edition.c.work_id.in_(chunk), unit.c.track == "main", openable,
                visible_editions_clause(scope),
            )
            .subquery()
        )  # fmt: skip
        found.update(
            (str(r.work_id), str(r.id))
            for r in connection.execute(sa.select(under_way).where(under_way.c.rn == 1))
        )
        rest = [w for w in chunk if w not in found]
        if not rest:
            continue
        first = (
            sa.select(
                edition.c.work_id,
                unit.c.id,
                sa.func.row_number()
                .over(partition_by=edition.c.work_id, order_by=(edition.c.id, *order))
                .label("rn"),
            )
            .select_from(unit.join(edition, edition.c.id == unit.c.edition_id))
            .where(edition.c.work_id.in_(rest), openable, visible_editions_clause(scope))
            .subquery()
        )
        found.update(
            (str(r.work_id), str(r.id))
            for r in connection.execute(sa.select(first).where(first.c.rn == 1))
        )
    return found
