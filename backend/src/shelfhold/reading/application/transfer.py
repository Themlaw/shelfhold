"""A change of preferred Edition carries each profile's reading over (EF-BIB-026, EF-BIB-044).

For every profile that read the Work in another Edition, the furthest part it reached becomes a
neutral position (`reading.domain.neutral_position`) and is written in the new preferred Edition
as a `transposed` position on the file that carries the corresponding Unit: no page (a page of
one cut means nothing in another), a share of the file, and `approximate` in `reading_progress`.

What it never does, because a guess is not a proof:

* it marks no Unit the profile did not read: a Unit is journalled read (`read_units`, so that the
  member can undo it) only when the old Edition said it was read, by its number; a chapter the
  reader skipped stays unread;
* it is not the start of a move: `_mark_traversed` leaves a `transposed` position out of the
  chain it measures a speed on;
* it is not told to Kavita: the synchronisation leaves it out of the pages it pushes and of the
  marks it relays, so Kavita never receives a "read" that nobody made.

Each profile is carried over by its own reading and by its own scope: a profile that may not see
the new Edition (a child, above her ceiling), or that read nothing, gets nothing; nobody's reading
is ever given to another. The old positions are kept, so that a return to the old Edition finds
the reader where it left.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog

from shelfhold.catalog.public import (
    EditionPart,
    PreferredChange,
    edition_parts,
    set_preferred_edition,
    visible_edition_ids,
)
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.schema import export, reading
from shelfhold.reading.application.progress import (
    _coverage,
    _current_hash,
    insert_position,
    progress_of,
)
from shelfhold.reading.domain.neutral_position import (
    NeutralPosition,
    SourcePart,
    neutral_of,
    transpose,
)
from shelfhold.reading.domain.projection import Bounds, project

log = structlog.get_logger(__name__)

__all__ = ["TransferReport", "carry_over", "prefer_edition"]

DEVICE: str = "transfer"  # the `device_id` of a transposed position: no real device is this one
_MIN_SHARE = 0.01  # a part arrived at its very start is started all the same


@dataclass(frozen=True)
class TransferReport:
    work_id: str
    edition_id: str
    transferred: int  # profiles whose reading was written in the new Edition
    pending: int  # profiles whose reading has no file to land on yet


def prefer_edition(connection: sa.Connection, *, edition_id: str, now: datetime) -> TransferReport:
    """Make the Edition the preferred one, and carry each profile's reading over to it."""
    change = set_preferred_edition(connection, edition_id)
    return carry_over(connection, change, now=now)


def carry_over(
    connection: sa.Connection, change: PreferredChange, *, now: datetime
) -> TransferReport:
    target = edition_parts(connection, change.edition_id)
    sources = {other: edition_parts(connection, other) for other in change.others}
    sources = {k: v for k, v in sources.items() if v}
    transferred = pending = 0
    if target and sources:
        for user_id in _readers(connection, sources):
            scope = reader_scope(connection, user_id)
            if scope.kind == "none":
                continue
            if change.edition_id not in visible_edition_ids(connection, scope, [change.edition_id]):
                continue
            found = _neutral(connection, user_id, change.work_id, sources)
            if found is None:
                continue
            neutral, read_numbers = found
            wrote = _land(
                connection, user_id, change.edition_id, neutral, read_numbers, target, now
            )
            transferred += wrote is True
            pending += wrote is None
    log.info("reading.edition_transferred", work=change.work_id, profiles=transferred)
    return TransferReport(change.work_id, change.edition_id, transferred, pending)


def _readers(connection: sa.Connection, sources: dict[str, list[EditionPart]]) -> list[str]:
    units = [p.unit_id for parts in sources.values() for p in parts]
    view = sa.table("reading_progress", sa.column("user_id"), sa.column("unit_id"))
    found: set[str] = set()
    for start in range(0, len(units), 500):
        found.update(
            str(r[0])
            for r in connection.execute(
                sa.select(view.c.user_id).where(view.c.unit_id.in_(units[start : start + 500]))
            )
        )
    return sorted(found)


def _neutral(
    connection: sa.Connection, user_id: str, work_id: str, sources: dict[str, list[EditionPart]]
) -> tuple[NeutralPosition, set[float]] | None:
    """The furthest neutral position over the old Editions the profile may see, and the numbers it
    read in that Edition."""
    best: tuple[NeutralPosition, set[float]] | None = None
    for parts in sources.values():
        rows = {
            r.unit_id: r
            for r in progress_of(
                connection, viewer_id=user_id, subject_id=user_id,
                unit_ids=[p.unit_id for p in parts],
            )
        }  # fmt: skip
        source = []
        for p in parts:
            row = rows.get(p.unit_id)
            source.append(
                SourcePart(
                    p.number,
                    row.state if row else "unread",
                    _within(connection, row.last_position_id, p.unit_id) if row else None,
                )
            )
        neutral = neutral_of(work_id, source)
        if neutral is None:
            continue
        read = {s.number for s in source if s.state == "read"}
        if best is None or (neutral.completed, neutral.total_progression) > (
            best[0].completed, best[0].total_progression,
        ):  # fmt: skip
            best = (neutral, read)
    return best


def _within(connection: sa.Connection, position_id: str, unit_id: str) -> float | None:
    """How far in the Unit the position says it is: the rule of the view, on this position."""
    table = reading.reading_position
    row = connection.execute(sa.select(table).where(table.c.id == position_id)).first()
    if row is None:
        return None
    cover = _coverage(connection, row.carrier_type, row.carrier_id)
    ids = [str(c.unit_id) for c in cover]
    if unit_id not in ids:
        return None
    index = ids.index(unit_id)
    current = _current_hash(connection, row.carrier_type, row.carrier_id)
    page = row.locator.get("page")
    first, last = cover[index].start_page, cover[index].end_page
    done = project(
        page=page if isinstance(page, int) and not isinstance(page, bool) else None,
        percent=row.percent,
        bounds=Bounds(
            first if isinstance(first, int) else None,
            last if isinstance(last, int) else None,
            index,
            len(cover),
        ),
        replaced=current is not None and current != row.content_hash,
        trusted=row.source == "server",
        transposed=row.locator.get("transposed") is True,
    )
    return done.within


def _carrier_of(connection: sa.Connection, edition: str, unit_id: str) -> str | None:
    """The file of the Edition that carries the Unit (the first, by id) and has a content."""
    cover, item = export.unit_coverage, export.export_item
    for found in connection.execute(
        sa.select(item.c.id)
        .select_from(cover.join(item, item.c.id == cover.c.carrier_id))
        .where(
            cover.c.carrier_type == "export_item", cover.c.unit_id == unit_id,
            item.c.edition_id == edition,
        )
        .order_by(item.c.id)
    ):  # fmt: skip
        if _current_hash(connection, "export_item", str(found.id)) is not None:
            return str(found.id)
    return None


def _land(
    connection: sa.Connection,
    user_id: str,
    edition: str,
    neutral: NeutralPosition,
    read_numbers: set[float],
    target: list[EditionPart],
    now: datetime,
) -> bool | None:
    """Write the neutral position in the new Edition. `True` when something was written, `None`
    when the part it lands on has no file yet, `False` when it was already there."""
    landed = transpose(neutral, [p.number for p in target])
    if landed is None:
        return False
    index, within = landed
    arrival = target[index]
    marks = {p.unit_id for p in target if p.number in read_numbers}
    if neutral.completed:
        marks.add(arrival.unit_id)
    reached: dict[str, float] = {u: 1.0 for u in marks}
    reached.setdefault(arrival.unit_id, max(within, _MIN_SHARE))
    carriers: dict[str, list[str]] = {}
    for unit_id in reached:
        carrier = _carrier_of(connection, edition, unit_id)
        if carrier is None:
            if unit_id == arrival.unit_id:
                return None  # nowhere to put the position: nothing is invented
            marks.discard(unit_id)
            continue
        carriers.setdefault(carrier, []).append(unit_id)
    wrote = False
    for carrier, units in carriers.items():
        wrote |= _write(connection, user_id, carrier, units, reached, marks, neutral, now)
    return wrote


def _write(
    connection: sa.Connection,
    user_id: str,
    carrier: str,
    units: list[str],
    reached: dict[str, float],
    marks: set[str],
    neutral: NeutralPosition,
    now: datetime,
) -> bool:
    cover = [str(c.unit_id) for c in _coverage(connection, "export_item", carrier)]
    last = max(cover.index(u) for u in units if u in cover)
    share = reached[cover[last]]
    percent = min(1.0, (last + share) / len(cover))
    locator: dict[str, Any] = {"transposed": True, "neutral": neutral.as_json()}
    table = reading.reading_position
    if _already(connection, user_id, carrier, locator, percent):
        return False
    content_hash = _current_hash(connection, "export_item", carrier)
    if content_hash is None:  # `_carrier_of` only gives files that have one: a file that lost it
        return False
    position = insert_position(
        connection, user_id=user_id, carrier_type="export_item", carrier_id=carrier,
        content_hash=content_hash, locator=locator, percent=percent,
        client_ts=now if now.tzinfo else now.replace(tzinfo=UTC), device_id=DEVICE,
        source="device", now=now, traverse=False,
    )  # fmt: skip
    read = [u for u in cover if u in marks]
    if read:
        connection.execute(sa.update(table).where(table.c.id == position).values(read_units=read))
    return True


def _already(
    connection: sa.Connection, user_id: str, carrier: str, locator: dict[str, Any], percent: float
) -> bool:
    """The same neutral position was already carried onto this file: a second call adds nothing."""
    table = reading.reading_position
    for row in connection.execute(
        sa.select(table.c.locator, table.c.percent).where(
            table.c.user_id == user_id,
            table.c.carrier_type == "export_item",
            table.c.carrier_id == carrier,
            table.c.device_id == DEVICE,
        )
    ):
        if row.locator == locator and row.percent == percent:
            return True
    return False
