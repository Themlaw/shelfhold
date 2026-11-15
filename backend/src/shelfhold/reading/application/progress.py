"""Who records and who reads a reading position (EF-LEC-014, EF-USR-006, ENF-SEC-010).

* A profile records and reads **its own** positions and progress, nobody else's, admin included.
  The reader comes from the authenticated principal (a session, a key or a device token all read
  as the profile they were issued for, `identity.reader_scope`); `subject_id` is what the request
  asks for. When the two differ the answer is the one for something that is not there: nothing.
* The scope of the reader is read from the database at each call: a ceiling lowered, a profile
  removed, take effect at once. A profile that is not there reads and writes nothing.
* A child sees no progress, and records no position, on a Work above her ceiling; a carrier she may
  not see answers exactly like one that does not exist (`UnknownCarrierError`, one message).
* A client never writes the progress: it records a position, and `reading_progress` is recomputed
  from it (the merge of `reading.domain.merge`). `client_ts` is stored bounded by `server_ts`.
* The page of a locator is normalised on the way in: an integer (a number written in text or a
  whole float becomes one), else `InvalidLocatorError` (a 422 for the routes). The database
  refuses any other page.
* A `device` position never makes a Unit read by itself. When it completes a page-by-page
  traversal (`reading.domain.traversal`), the Units crossed are marked read in `read_units` of
  that position: a journal (logged), that the member can undo (`undo_read_mark`).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import sqlalchemy as sa
import structlog

from shelfhold.catalog.public import visible_edition_ids, visible_unit_ids
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog as catalog_schema
from shelfhold.infrastructure.schema import export, reading, storage
from shelfhold.reading.domain.merge import bounded_ts
from shelfhold.reading.domain.traversal import PAGES, SHARE, Span, Step, traversed_units

log = structlog.get_logger(__name__)

__all__ = [
    "InvalidLocatorError",
    "PositionRecord",
    "UnitProgress",
    "UnknownCarrierError",
    "normalise_locator",
    "position_of",
    "progress_of",
    "record_position",
    "undo_read_mark",
]


class UnknownCarrierError(LookupError):
    """The file or content does not exist, or the reader may not see it: the same answer."""

    def __init__(self) -> None:
        super().__init__("unknown carrier")


class InvalidLocatorError(ValueError):
    """The page of a locator is not an integer (a 422 for the routes)."""


_MAX_PAGE = 10**9
_DIGITS = re.compile(r"[0-9]{1,10}")


def normalise_locator(locator: dict[str, Any]) -> dict[str, Any]:
    """The locator with its `page` (if any) as an integer: an integer stays, a whole float or a
    number in text becomes one, `null` is no page. Anything else raises `InvalidLocatorError`."""
    out = dict(locator)
    if "page" not in out:
        return out
    page = out["page"]
    if page is None:
        del out["page"]
        return out
    if isinstance(page, bool):
        raise InvalidLocatorError("page is an integer")
    if isinstance(page, float) and page.is_integer():
        page = int(page)
    elif isinstance(page, str) and _DIGITS.fullmatch(page.strip()):
        page = int(page.strip())
    if not isinstance(page, int) or not 0 <= page <= _MAX_PAGE:
        raise InvalidLocatorError("page is an integer")
    out["page"] = page
    return out


@dataclass(frozen=True)
class UnitProgress:
    unit_id: str
    state: str  # unread | in_progress | read
    last_position_id: str  # the furthest position
    approximate: bool
    latest_position_id: str | None = None  # the last position written


@dataclass(frozen=True)
class PositionRecord:
    id: str
    carrier_type: str
    carrier_id: str
    content_hash: str
    percent: float | None
    client_ts: datetime
    server_ts: datetime
    device_id: str | None


def _edition_of(connection: sa.Connection, carrier_type: str, carrier_id: str) -> str | None:
    if carrier_type == "export_item":
        query = sa.select(export.export_item.c.edition_id).where(
            export.export_item.c.id == carrier_id
        )
    elif carrier_type == "content":
        query = (
            sa.select(catalog_schema.unit.c.edition_id)
            .select_from(
                storage.content.join(
                    catalog_schema.unit, catalog_schema.unit.c.id == storage.content.c.unit_id
                )
            )
            .where(storage.content.c.id == carrier_id)
        )
    else:
        return None
    found = connection.execute(query).scalar()
    return None if found is None else str(found)


def _ts(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _coverage(connection: sa.Connection, carrier_type: str, carrier_id: str) -> list[Any]:
    cover = export.unit_coverage
    return list(
        connection.execute(
            sa.select(
                cover.c.unit_id,
                sa.func.json_extract(cover.c.start_locator, "$.page").label("start_page"),
                sa.func.json_extract(cover.c.end_locator, "$.page").label("end_page"),
            )
            .where(cover.c.carrier_type == carrier_type, cover.c.carrier_id == carrier_id)
            .order_by(cover.c.position)
        )
    )


def _current_hash(connection: sa.Connection, carrier_type: str, carrier_id: str) -> str | None:
    """The content hash the carrier carries now (what the view calls not replaced)."""
    if carrier_type == "content":
        found = connection.execute(
            sa.select(storage.content.c.content_hash).where(storage.content.c.id == carrier_id)
        ).scalar()
        return None if found is None else str(found)
    found = connection.execute(
        sa.select(storage.content.c.content_hash)
        .select_from(
            export.export_item.join(
                storage.content, storage.content.c.id == export.export_item.c.content_id
            )
        )
        .where(export.export_item.c.id == carrier_id)
    ).scalar()
    if found is None:
        alias = export.document_alias
        found = connection.execute(
            sa.select(alias.c.content_hash)
            .where(alias.c.export_item_id == carrier_id)
            .order_by(alias.c.created_at.desc(), alias.c.file_hash.desc())
            .limit(1)
        ).scalar()
    return None if found is None else str(found)


def _mark_traversed(
    connection: sa.Connection, position_id: str, now: datetime, *, history_limit: int = 1000
) -> list[str]:
    """Mark read the Units the chain of this device's positions has now traversed (ADR-0053).

    Only on the content the carrier carries now, from `device` positions of the same device; a
    Unit already marked, or whose mark was undone after the evidence, is left alone."""
    table = reading.reading_position
    row = connection.execute(sa.select(table).where(table.c.id == position_id)).one()
    current = _current_hash(connection, row.carrier_type, row.carrier_id)
    if current is not None and current != row.content_hash:
        return []
    cover = _coverage(connection, row.carrier_type, row.carrier_id)
    if not cover:
        return []
    paged = all(isinstance(c.start_page, int) and isinstance(c.end_page, int) for c in cover)
    if paged and isinstance(row.locator.get("page"), int):
        mode = PAGES
        spans = [Span(c.unit_id, float(c.start_page), float(c.end_page)) for c in cover]
    elif len(cover) == 1 and row.percent is not None:
        mode = SHARE
        spans = [Span(cover[0].unit_id, 0.0, 1.0)]
    else:
        return []
    history = connection.execute(
        sa.select(table)
        .where(
            table.c.user_id == row.user_id,
            table.c.carrier_type == row.carrier_type,
            table.c.carrier_id == row.carrier_id,
            table.c.content_hash == row.content_hash,
            table.c.source == "device",
            table.c.device_id.is_(None)
            if row.device_id is None
            else table.c.device_id == row.device_id,
        )
        .order_by(table.c.server_ts.desc(), table.c.id.desc())
        .limit(history_limit)
    ).all()
    steps = [
        (bounded_ts(_ts(h.client_ts), _ts(h.server_ts)), h.id, h.locator.get("page"), h.percent)
        for h in history
    ]
    steps.sort(key=lambda t: (t[0], t[1]))
    chain = [
        Step(float(page if mode == PAGES else percent), ts)
        for ts, _id, page, percent in steps
        if (isinstance(page, int) if mode == PAGES else percent is not None)
    ]
    spans_ids = {span.unit_id for span in spans}
    after: dict[str, datetime] = {}
    already: set[str] = set()
    for other in connection.execute(
        sa.select(table.c.read_units, table.c.undone_units).where(
            table.c.user_id == row.user_id, sa.func.json_array_length(table.c.read_units) > 0
        )
    ):
        for unit, when in other.undone_units.items():
            if unit in spans_ids:
                after[unit] = max(
                    after.get(unit, _ts(datetime.fromisoformat(when))),
                    _ts(datetime.fromisoformat(when)),
                )
        already.update(u for u in other.read_units if u not in other.undone_units)
    marked = [u for u in traversed_units(chain, spans, mode=mode, after=after) if u not in already]
    if marked:
        connection.execute(
            sa.update(table).where(table.c.id == position_id).values(read_units=marked)
        )
        log.info("reading.read_marked", position=position_id, units=len(marked))
    return marked


def record_position(
    connection: sa.Connection,
    *,
    reader_id: str,
    subject_id: str,
    carrier_type: str,
    carrier_id: str,
    content_hash: str,
    locator: dict[str, Any],
    percent: float | None,
    client_ts: datetime,
    device_id: str | None = None,
    source: Literal["device", "server"] = "device",
    now: datetime | None = None,
) -> str:
    """Record where `subject_id` is in a file, as that same reader; returns the position id.

    `source="server"` is for the read state a server keeps itself (the pull from Kavita): it is
    taken as it is. A reader's own report (KOReader, the integrated reader, a device token) is
    `device`, the default: it marks a Unit read only by a page-by-page traversal."""
    if reader_id != subject_id:
        raise UnknownCarrierError
    scope = reader_scope(connection, reader_id)
    edition = _edition_of(connection, carrier_type, carrier_id)
    if edition is None or edition not in visible_edition_ids(connection, scope, [edition]):
        raise UnknownCarrierError
    locator = normalise_locator(locator)
    if percent is not None and not 0.0 <= percent <= 1.0:
        raise ValueError("percent is between 0 and 1")
    server_ts = now or datetime.now(UTC)
    position_id = new_ulid()
    connection.execute(
        sa.insert(reading.reading_position).values(
            id=position_id, user_id=reader_id, carrier_type=carrier_type, carrier_id=carrier_id,
            content_hash=content_hash, locator=locator, percent=percent,
            client_ts=bounded_ts(client_ts, server_ts), server_ts=server_ts, device_id=device_id,
            source=source,
            covered_units=[c.unit_id for c in _coverage(connection, carrier_type, carrier_id)],
        )
    )  # fmt: skip
    if source == "device":
        _mark_traversed(connection, position_id, server_ts)
    return position_id


def undo_read_mark(
    connection: sa.Connection,
    *,
    viewer_id: str,
    subject_id: str,
    unit_id: str,
    now: datetime | None = None,
) -> int:
    """Undo the traversal mark of a Unit, as the member it belongs to (nobody else, admin
    included: the answer is the one for nothing to undo). Returns how many marks were undone."""
    if viewer_id != subject_id:
        return 0
    scope = reader_scope(connection, viewer_id)
    if scope.kind == "none" or unit_id not in visible_unit_ids(connection, scope, [unit_id]):
        return 0
    when = _ts(now or datetime.now(UTC)).isoformat()
    table = reading.reading_position
    undone = 0
    for row in connection.execute(
        sa.select(table.c.id, table.c.read_units, table.c.undone_units).where(
            table.c.user_id == viewer_id, sa.func.json_array_length(table.c.read_units) > 0
        )
    ).all():
        if unit_id in row.read_units and unit_id not in row.undone_units:
            connection.execute(
                sa.update(table)
                .where(table.c.id == row.id)
                .values(undone_units={**row.undone_units, unit_id: when})
            )
            undone += 1
    if undone:
        log.info("reading.read_unmarked", unit=unit_id, marks=undone)
    return undone


def progress_of(
    connection: sa.Connection,
    *,
    viewer_id: str,
    subject_id: str,
    unit_ids: Iterable[str] | None = None,
) -> list[UnitProgress]:
    """The progress of `subject_id` per Unit, for a viewer who is that profile: else nothing."""
    if viewer_id != subject_id:
        return []
    scope = reader_scope(connection, viewer_id)
    if scope.kind == "none":
        return []
    view = sa.table(
        "reading_progress",
        sa.column("user_id"), sa.column("unit_id"), sa.column("state"),
        sa.column("last_position_id"), sa.column("latest_position_id"),
        sa.column("approximate"),
    )  # fmt: skip
    query = sa.select(view).where(view.c.user_id == viewer_id).order_by(view.c.unit_id)
    wanted = None if unit_ids is None else sorted(set(unit_ids))
    if wanted is not None:
        if not wanted:
            return []
        query = query.where(view.c.unit_id.in_(wanted))
    rows = connection.execute(query).all()
    allowed = (
        {r.unit_id for r in rows}
        if scope.unrestricted
        else visible_unit_ids(connection, scope, [r.unit_id for r in rows])
    )
    return [
        UnitProgress(
            r.unit_id, r.state, r.last_position_id, bool(r.approximate), r.latest_position_id
        )
        for r in rows
        if r.unit_id in allowed
    ]


def position_of(
    connection: sa.Connection, *, viewer_id: str, position_id: str
) -> PositionRecord | None:
    """One of the viewer's own positions, or None (also for another profile's, for a position on
    a Work above her ceiling and for one that is not there)."""
    scope = reader_scope(connection, viewer_id)
    if scope.kind == "none":
        return None
    table = reading.reading_position
    row = connection.execute(
        sa.select(table).where(table.c.id == position_id, table.c.user_id == viewer_id)
    ).first()
    if row is None:
        return None
    edition = _edition_of(connection, row.carrier_type, row.carrier_id)
    if edition is None:
        seen = scope.unrestricted  # the file is gone: only an adult still has her own history
    else:
        seen = edition in visible_edition_ids(connection, scope, [edition])
    if not seen:
        return None
    return PositionRecord(
        row.id, row.carrier_type, row.carrier_id, row.content_hash, row.percent,
        row.client_ts, row.server_ts, row.device_id,
    )  # fmt: skip
