"""The preferred Edition of a Work (EF-BIB-026), and what two Editions of it have in common.

One Edition of a Work is the preferred one (the language or the translation the reader is given
first). Making another one preferred moves a flag and nothing else: no file is replaced, no
Unit is touched. The reading of each profile is carried over by the reading context
(`reading.application.transfer`), which asks this module which Units an Edition is made of, in
reading order, and by which number a Unit corresponds to its twin in another Edition
(EF-BIB-044: the number is what a volume and its chapters share; the files that carry the Units
and the bounds of each Unit in them are `unit_coverage`).
"""

from dataclasses import dataclass

import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog

__all__ = ["EditionPart", "PreferredChange", "edition_parts", "set_preferred_edition"]


@dataclass(frozen=True)
class PreferredChange:
    work_id: str
    edition_id: str
    others: tuple[str, ...]  # the other Editions of the Work, where a reader may have read


@dataclass(frozen=True)
class EditionPart:
    unit_id: str
    number: float


def set_preferred_edition(connection: sa.Connection, edition_id: str) -> PreferredChange:
    """Make `edition_id` the preferred Edition of its Work: the one that was is no longer.

    `LookupError` for an Edition that does not exist. Calling it for the one that already is
    changes nothing."""
    table = catalog.edition
    work_id = connection.execute(
        sa.select(table.c.work_id).where(table.c.id == edition_id)
    ).scalar()
    if work_id is None:
        raise LookupError("no such Edition")
    connection.execute(
        sa.update(table)
        .where(table.c.work_id == work_id, table.c.id != edition_id)
        .values(preferred=False)
    )
    connection.execute(sa.update(table).where(table.c.id == edition_id).values(preferred=True))
    others = connection.execute(
        sa.select(table.c.id)
        .where(table.c.work_id == work_id, table.c.id != edition_id)
        .order_by(table.c.id)
    ).scalars()
    return PreferredChange(str(work_id), edition_id, tuple(str(o) for o in others))


def edition_parts(connection: sa.Connection, edition_id: str) -> list[EditionPart]:
    """The numbered Units of the main track of an Edition, in reading order. A Unit with no number
    (an extra, a special) has no twin to find: it is left out."""
    unit = catalog.unit
    rows = connection.execute(
        sa.select(unit.c.id, unit.c.number_sort)
        .where(
            unit.c.edition_id == edition_id,
            unit.c.track == "main",
            unit.c.number_sort.is_not(None),
        )
        .order_by(unit.c.number_sort, unit.c.number_segment_ordinal, unit.c.source_order, unit.c.id)
    )
    return [EditionPart(str(r.id), float(r.number_sort)) for r in rows]
