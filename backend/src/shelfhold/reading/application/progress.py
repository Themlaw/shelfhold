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
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from shelfhold.catalog.public import visible_edition_ids, visible_unit_ids
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog as catalog_schema
from shelfhold.infrastructure.schema import export, reading, storage
from shelfhold.reading.domain.merge import bounded_ts

__all__ = [
    "PositionRecord",
    "UnitProgress",
    "UnknownCarrierError",
    "position_of",
    "progress_of",
    "record_position",
]


class UnknownCarrierError(LookupError):
    """The file or content does not exist, or the reader may not see it: the same answer."""

    def __init__(self) -> None:
        super().__init__("unknown carrier")


@dataclass(frozen=True)
class UnitProgress:
    unit_id: str
    state: str  # unread | in_progress | read
    last_position_id: str
    approximate: bool


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
) -> str:
    """Record where `subject_id` is in a file, as that same reader; returns the position id."""
    if reader_id != subject_id:
        raise UnknownCarrierError
    scope = reader_scope(connection, reader_id)
    edition = _edition_of(connection, carrier_type, carrier_id)
    if edition is None or edition not in visible_edition_ids(connection, scope, [edition]):
        raise UnknownCarrierError
    if percent is not None and not 0.0 <= percent <= 1.0:
        raise ValueError("percent is between 0 and 1")
    server_ts = now or datetime.now(UTC)
    position_id = new_ulid()
    connection.execute(
        sa.insert(reading.reading_position).values(
            id=position_id, user_id=reader_id, carrier_type=carrier_type, carrier_id=carrier_id,
            content_hash=content_hash, locator=locator, percent=percent,
            client_ts=bounded_ts(client_ts, server_ts), server_ts=server_ts, device_id=device_id,
        )
    )  # fmt: skip
    return position_id


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
        sa.column("last_position_id"), sa.column("approximate"),
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
        UnitProgress(r.unit_id, r.state, r.last_position_id, bool(r.approximate))
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
