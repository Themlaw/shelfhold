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
from typing import Any

import sqlalchemy as sa
import structlog

from shelfhold.catalog.public import visible_edition_ids, visible_unit_ids
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog as catalog_schema
from shelfhold.infrastructure.schema import export, reading, storage
from shelfhold.reading.domain.merge import bounded_ts
from shelfhold.reading.domain.traversal import (
    DEFAULT_MAX_PAGES_PER_MINUTE,
    PAGES,
    SHARE,
    Span,
    Step,
    traversed_units,
)

log = structlog.get_logger(__name__)

HISTORY_LIMIT = 1000

__all__ = [
    "InvalidLocatorError",
    "PositionRecord",
    "UnitProgress",
    "UnknownCarrierError",
    "mark_traversed_in_frame",
    "normalise_locator",
    "position_of",
    "progress_of",
    "record_delivery",
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


def _page_count(connection: sa.Connection, carrier_type: str, carrier_id: str) -> int | None:
    content = storage.content
    if carrier_type == "content":
        query = sa.select(content.c.page_count).where(content.c.id == carrier_id)
    else:
        query = (
            sa.select(content.c.page_count)
            .select_from(
                export.export_item.join(content, content.c.id == export.export_item.c.content_id)
            )
            .where(export.export_item.c.id == carrier_id)
        )
    found = connection.execute(query).scalar()
    return int(found) if isinstance(found, int) and found > 0 else None


def snapshot_covered_units(
    connection: sa.Connection, carrier_type: str, carrier_id: str, content_hash: str
) -> list[str] | None:
    """The Units a position on `content_hash` reaches: what that content covered.

    On the content the carrier carries now, the coverage now. On another one (a device that kept
    the file that was replaced) the coverage is not known any more: it is taken from an earlier
    position on that very content, and without one the position reaches nothing (`None`). It never
    takes the coverage of the content that replaced it."""
    current = _current_hash(connection, carrier_type, carrier_id)
    if current is None or current == content_hash:
        return [str(c.unit_id) for c in _coverage(connection, carrier_type, carrier_id)]
    table = reading.reading_position
    earlier = connection.execute(
        sa.select(table.c.covered_units)
        .where(
            table.c.carrier_type == carrier_type,
            table.c.carrier_id == carrier_id,
            table.c.content_hash == content_hash,
            table.c.covered_units.is_not(None),
        )
        .order_by(table.c.server_ts.asc(), table.c.id.asc())
        .limit(1)
    ).scalar()
    return None if earlier is None else [str(u) for u in earlier]


def _mark_state(
    connection: sa.Connection, user_id: str, unit_ids: set[str]
) -> tuple[dict[str, datetime], set[str]]:
    """What the profile's earlier marks say of these Units: when a mark was undone (a later move
    only counts again), and which Units are already marked and not undone."""
    table = reading.reading_position
    after: dict[str, datetime] = {}
    already: set[str] = set()
    for other in connection.execute(
        sa.select(table.c.read_units, table.c.undone_units).where(
            table.c.user_id == user_id,
            sa.or_(
                sa.func.json_array_length(table.c.read_units) > 0,
                table.c.undone_units != "{}",
            ),
        )
    ):
        for unit, when in other.undone_units.items():
            if unit in unit_ids:
                stamp = _ts(datetime.fromisoformat(when))
                after[unit] = max(after.get(unit, stamp), stamp)
        already.update(u for u in other.read_units if u not in other.undone_units)
    return after, already


def mark_traversed_in_frame(
    connection: sa.Connection,
    position_id: str,
    *,
    steps: list[Step],
    spans: list[Span],
    max_pages_per_minute: float = DEFAULT_MAX_PAGES_PER_MINUTE,
) -> list[str]:
    """The traversal of a frame that is not one file's: the pages of a block of a series, whose
    chapters sit on several files. `steps` are the profile's positions in that frame (page of the
    block, `server_ts`), oldest first, and `spans` the Units' ranges of pages in it. The rule is the
    one of `_mark_traversed` (speed on `server_ts` alone, no instant jump, undoable); the marks go
    in the journal of the position that closed the move."""
    row = connection.execute(
        sa.select(reading.reading_position.c.user_id).where(
            reading.reading_position.c.id == position_id
        )
    ).one()
    after, already = _mark_state(connection, row.user_id, {span.unit_id for span in spans})
    marked = [
        u
        for u in traversed_units(
            steps, spans, mode=PAGES, max_pages_per_minute=max_pages_per_minute, after=after
        )
        if u not in already
    ]
    if marked:
        connection.execute(
            sa.update(reading.reading_position)
            .where(reading.reading_position.c.id == position_id)
            .values(read_units=marked)
        )
        log.info("reading.read_marked", position=position_id, units=len(marked))
    return marked


def _mark_traversed(
    connection: sa.Connection, position_id: str, max_pages_per_minute: float
) -> list[str]:
    """Mark read the Units the plausible moves of this profile on this content now cover
    (`reading.domain.traversal`: a speed rule on `server_ts`, ADR-0053 as amended).

    Only on the content the carrier carries now; a Unit already marked, or whose mark was undone
    after the evidence, is left alone."""
    table = reading.reading_position
    row = connection.execute(sa.select(table).where(table.c.id == position_id)).one()
    current = _current_hash(connection, row.carrier_type, row.carrier_id)
    if current is not None and current != row.content_hash:
        return []
    cover = _coverage(connection, row.carrier_type, row.carrier_id)
    if not cover:
        return []
    if row.locator.get("xpointer") is not None and (
        row.locator.get("end") is not True or not isinstance(row.locator.get("who"), str)
    ):
        return []  # a place in a reflowable text: read only at the end of its last document (L09)
    paged = all(isinstance(c.start_page, int) and isinstance(c.end_page, int) for c in cover)
    scale: float | None = None
    if paged and isinstance(row.locator.get("page"), int):
        mode = PAGES
        spans = [Span(c.unit_id, float(c.start_page), float(c.end_page)) for c in cover]
    elif len(cover) == 1 and row.percent is not None:
        mode = SHARE
        spans = [Span(cover[0].unit_id, 0.0, 1.0)]
        count = _page_count(connection, row.carrier_type, row.carrier_id)
        # a place in a reflowable text is not paged: the page count of an EPUB is its number of
        # sections, never a speed to judge a share by (a leap to the end would pass for a read)
        is_text = row.locator.get("xpointer") is not None
        scale = None if count is None or is_text else float(count)
    else:
        return []
    history = list(
        connection.execute(
            sa.select(table)
            .where(
                table.c.user_id == row.user_id,
                table.c.carrier_type == row.carrier_type,
                table.c.carrier_id == row.carrier_id,
                table.c.content_hash == row.content_hash,
                table.c.source == "device",
                *_same_writer(table, row.locator),
                # nor is a position carried over from another Edition (EF-BIB-026): a guess
                sa.func.json_extract(table.c.locator, "$.transposed").is_(None),
            )
            .order_by(table.c.server_ts.desc(), table.c.id.desc())
            .limit(HISTORY_LIMIT)
        )
    )
    history.reverse()  # oldest first: only the server's date orders them
    chain = [
        Step(float(h.locator["page"] if mode == PAGES else h.percent), _ts(h.server_ts))
        for h in history
        if (isinstance(h.locator.get("page"), int) if mode == PAGES else h.percent is not None)
    ]
    after, already = _mark_state(connection, row.user_id, {span.unit_id for span in spans})
    marked = [
        u
        for u in traversed_units(
            chain,
            spans,
            mode=mode,
            max_pages_per_minute=max_pages_per_minute,
            pages_per_unit_of_x=scale,
            after=after,
        )
        if u not in already
    ]
    if marked:
        connection.execute(
            sa.update(table).where(table.c.id == position_id).values(read_units=marked)
        )
        log.info("reading.read_marked", position=position_id, units=len(marked))
    return marked


def _same_writer(table: sa.Table, locator: dict[str, Any]) -> list[sa.ColumnElement[bool]]:
    """Which positions are the steps of a walk. For a position that is not an xpointer: the ones
    that are not either (a place in a reflowable text measures nothing there). For an xpointer: the
    xpointers of **that device** (`who`) whose document was read (`spine`): the positions of another
    device, or of another writer of the same file (the reader of the web), are never a step of its
    chain, and neither is its own place when it was not one of a document (M4 MIN-6, M5 L09)."""
    xpointer = sa.func.json_extract(table.c.locator, "$.xpointer")
    if locator.get("xpointer") is None:
        return [xpointer.is_(None)]
    return [
        xpointer.is_not(None),
        sa.func.json_extract(table.c.locator, "$.spine").is_not(None),
        sa.func.json_extract(table.c.locator, "$.who") == locator["who"],
    ]


def insert_position(
    connection: sa.Connection,
    *,
    user_id: str,
    carrier_type: str,
    carrier_id: str,
    content_hash: str,
    locator: dict[str, Any],
    percent: float | None,
    client_ts: datetime,
    device_id: str | None,
    source: str,
    now: datetime | None,
    max_pages_per_minute: float = DEFAULT_MAX_PAGES_PER_MINUTE,
    traverse: bool = True,
) -> str:
    """The one writer of `reading_position`, after the caller has checked who may write what.
    Internal to `reading.application`: `record_position` (a reader's own report) and
    `server_state.record_server_state` (the read state of a linked account) are its only
    callers, and only the first is public."""
    locator = normalise_locator(locator)
    if percent is not None and not 0.0 <= percent <= 1.0:
        raise ValueError("percent is between 0 and 1")
    server_ts = now or datetime.now(UTC)
    position_id = new_ulid()
    covered = snapshot_covered_units(connection, carrier_type, carrier_id, content_hash)
    connection.execute(
        sa.insert(reading.reading_position).values(
            id=position_id, user_id=user_id, carrier_type=carrier_type, carrier_id=carrier_id,
            content_hash=content_hash, locator=locator, percent=percent,
            client_ts=bounded_ts(client_ts, server_ts), server_ts=server_ts, device_id=device_id,
            source=source,
            covered_units=covered,
        )
    )  # fmt: skip
    if source == "device" and traverse:
        _mark_traversed(connection, position_id, max_pages_per_minute)
    return position_id


def check_carrier(
    connection: sa.Connection, user_id: str, carrier_type: str, carrier_id: str
) -> None:
    """The carrier exists and the profile may see it, else `UnknownCarrierError` (one answer)."""
    scope = reader_scope(connection, user_id)
    edition = _edition_of(connection, carrier_type, carrier_id)
    if edition is None or edition not in visible_edition_ids(connection, scope, [edition]):
        raise UnknownCarrierError


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
    now: datetime | None = None,
    max_pages_per_minute: float = DEFAULT_MAX_PAGES_PER_MINUTE,
    traverse: bool = True,
) -> str:
    """Record where `subject_id` is in a file, as that same reader; returns the position id.

    `traverse=False` stores the position and looks for no traversal: a locator the server cannot
    measure (an EPUB xpointer that is not the end of its book, M5 L09) is a place, never a proof of
    reading.

    This is a reader's own report (KOReader, the integrated reader, a device token): it never
    makes a Unit read by itself, only a plausible pass over it does (`reading.domain.traversal`).
    The read state a server keeps has its own door, `server_state.record_server_state`, which no
    interface imports."""
    if reader_id != subject_id:
        raise UnknownCarrierError
    check_carrier(connection, reader_id, carrier_type, carrier_id)
    return insert_position(
        connection, user_id=reader_id, carrier_type=carrier_type, carrier_id=carrier_id,
        content_hash=content_hash, locator=locator, percent=percent, client_ts=client_ts,
        device_id=device_id, source="device", now=now, max_pages_per_minute=max_pages_per_minute,
        traverse=traverse,
    )  # fmt: skip


def record_delivery(
    connection: sa.Connection,
    *,
    user_id: str,
    carrier_type: str,
    carrier_id: str,
    content_hash: str,
    now: datetime | None = None,
) -> str:
    """The file was handed to the profile (an OPDS download, the first known opening): page 0, the
    anchor from which the first move of a reader is measured. Called by the server with the
    authenticated profile, never with a value from a request."""
    check_carrier(connection, user_id, carrier_type, carrier_id)
    when = now or datetime.now(UTC)
    return insert_position(
        connection, user_id=user_id, carrier_type=carrier_type, carrier_id=carrier_id,
        content_hash=content_hash, locator={"page": 0}, percent=0.0, client_ts=when,
        device_id="delivery", source="device", now=when,
    )  # fmt: skip


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
        sa.select(
            table.c.id, table.c.read_units, table.c.undone_units, table.c.source,
            table.c.covered_units,
        ).where(
            table.c.user_id == viewer_id,
            sa.or_(
                sa.func.json_array_length(table.c.read_units) > 0,
                sa.and_(table.c.source == "server", table.c.covered_units.is_not(None)),
            ),
        )
    ).all():  # fmt: skip
        # a mark of a traversal, or a read state a server gave (a local overlay: it wins over that
        # state until a later position of the server gets past it)
        marked = unit_id in row.read_units or (
            row.source == "server" and unit_id in (row.covered_units or [])
        )
        if marked and unit_id not in row.undone_units:
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
    proven: bool = False,
) -> list[UnitProgress]:
    """The progress of `subject_id` per Unit, for a viewer who is that profile: else nothing.
    With `proven`, a position carried over from another Edition (EF-BIB-026) is left out."""
    if viewer_id != subject_id:
        return []
    scope = reader_scope(connection, viewer_id)
    if scope.kind == "none":
        return []
    view = sa.table(
        "reading_progress_proven" if proven else "reading_progress",
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
