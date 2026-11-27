"""The Units of an Edition, as the table of contents of the series sheet lists them (EF-USR-006).

* Reading order: the number, then its segment, then the order of the source (the order of the
  series bundle and of « Reprendre la lecture »); a Unit with no number comes last.
* Visibility is the filter of every other surface (`visible_editions_clause`), put in the query
  that reads the page and in the one that counts it: an Edition the profile may not see is exactly
  an Edition that does not exist (`UnknownEditionError`), and neither a page nor a total can carry
  one of its Units out. No new rule of visibility.
* The read state is the profile's own (`progress_of`, whose answer is nothing for anyone else).
* `readable`: the Unit has a file the integrated reader opens (the rule of the sheet's « Lire »).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

import sqlalchemy as sa

from shelfhold.catalog.public import visible_editions_clause
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.reading.application.progress import progress_of

__all__ = ["MAX_UNITS", "EditionUnit", "EditionUnitsPage", "UnknownEditionError", "edition_units"]

MAX_UNITS: Final = 200
READABLE_FORMATS: Final = ("cbz", "epub")  # what the reader opens (reading/application/publication)
ReadState = Literal["unread", "in_progress", "read"]
_STATES: Final = frozenset({"unread", "in_progress", "read"})


class UnknownEditionError(LookupError):
    """The Edition does not exist, or the profile may not see it: the same answer."""


@dataclass(frozen=True)
class EditionUnit:
    id: str
    number: str
    title: str | None
    kind: str
    track: str
    volume_id: str | None
    added_at: datetime
    read_state: ReadState
    readable: bool


@dataclass(frozen=True)
class EditionUnitsPage:
    edition_id: str
    items: list[EditionUnit]
    total: int


def edition_units(
    connection: sa.Connection, *, user_id: str, edition_id: str, limit: int, offset: int
) -> EditionUnitsPage:
    """One page of the Units of the Edition, for the profile `user_id`."""
    if not is_ulid(edition_id):
        raise UnknownEditionError(edition_id)
    scope = reader_scope(connection, user_id)
    edition = catalog.edition
    seen = connection.execute(
        sa.select(edition.c.id).where(edition.c.id == edition_id, visible_editions_clause(scope))
    ).first()
    if seen is None:
        raise UnknownEditionError(edition_id)
    unit = catalog.unit
    # the filter again in the query itself: a page is made of visible rows only
    where = (
        unit.c.edition_id == edition_id,
        unit.c.edition_id.in_(sa.select(edition.c.id).where(visible_editions_clause(scope))),
    )
    total = int(
        connection.execute(sa.select(sa.func.count()).select_from(unit).where(*where)).scalar_one()
    )
    readable = (
        sa.exists()
        .where(
            storage.content.c.unit_id == unit.c.id,
            storage.content.c.format.in_(READABLE_FORMATS),
        )
        .label("readable")
    )
    rows = connection.execute(
        sa.select(
            unit.c.id, unit.c.number_raw, unit.c.title, unit.c.kind, unit.c.track,
            unit.c.volume_id, unit.c.created_at, readable,
        )
        .where(*where)
        .order_by(
            unit.c.number_sort.is_(None), unit.c.number_sort, unit.c.number_segment_ordinal,
            unit.c.source_order, unit.c.id,
        )
        .limit(max(0, min(limit, MAX_UNITS)))
        .offset(max(0, offset))
    ).all()  # fmt: skip
    states = {
        p.unit_id: p.state
        for p in progress_of(
            connection, viewer_id=user_id, subject_id=user_id, unit_ids=[r.id for r in rows]
        )
    }
    return EditionUnitsPage(
        edition_id=edition_id,
        items=[
            EditionUnit(
                id=str(r.id), number=str(r.number_raw), title=r.title, kind=str(r.kind),
                track=str(r.track), volume_id=r.volume_id, added_at=r.created_at,
                read_state=_state(states.get(r.id)), readable=bool(r.readable),
            )
            for r in rows
        ],
        total=total,
    )  # fmt: skip


def _state(value: str | None) -> ReadState:
    return value if value in _STATES else "unread"  # type: ignore[return-value]
