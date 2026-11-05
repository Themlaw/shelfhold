"""Local value indicators (EF-INT-025): computed on the instance, from existing tables only.

`stats --since 30d` and `GET /api/v1/stats` read the same figures. Nothing is stored (no view,
no table, no migration) and nothing leaves the instance: the module opens no connection but the
database's (ENF-PRIV-001). The window is the last `since_days` days up to `now`.
"""

import re
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.events.operating import WAITING_STATE_EVENT
from shelfhold.infrastructure.schema import acquisition, catalog, export, platform, storage

MAX_SINCE_DAYS = 3650
DEFAULT_SINCE = "30d"
_SINCE = re.compile(r"^([1-9][0-9]{0,3})d$")

# Rows that count as an exported Unit, and the one that means "visible in Kavita": a scan found it.
EXPORTED_STATES = ("written", "scanned", "resolved")
VISIBLE_STATE = "resolved"
SENTINEL_EVENTS = ("mount.sentinel_missing",)
FOREIGN_WRITER_EVENTS = ("target.foreign_writer_detected",)
_WAITING_EVENTS = tuple(sorted(set(WAITING_STATE_EVENT.values())))


class InvalidSinceError(ValueError):
    """`--since` is not `<N>d` with N between 1 and 3650."""


def parse_since(text: str) -> int:
    match = _SINCE.match(text.strip())
    days = int(match.group(1)) if match else 0
    if not match or days > MAX_SINCE_DAYS:
        raise InvalidSinceError(text)
    return days


@dataclass(frozen=True)
class Stats:
    since_days: int
    human_interventions: dict[str, int]
    units_exported: int
    median_detection_to_visible_s: int | None
    guards_triggered: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _events(connection: sa.Connection, types: tuple[str, ...], since: datetime) -> int:
    event = platform.event
    return int(
        connection.execute(
            sa.select(sa.func.count()).where(event.c.type.in_(types), event.c.occurred_at >= since)
        ).scalar_one()
    )


def compute_stats(engine: Engine, *, now: datetime, since_days: int) -> Stats:
    since = now - timedelta(days=since_days)
    record = export.export_record
    with engine.connect() as connection:
        replays = connection.execute(
            sa.select(sa.func.count()).where(
                acquisition.revision_pending.c.state != "open",
                acquisition.revision_pending.c.resolved_at >= since,
            )
        ).scalar_one()
        exported = connection.execute(
            sa.select(sa.func.count()).where(
                record.c.state.in_(EXPORTED_STATES), record.c.exported_at >= since
            )
        ).scalar_one()
        # Detection (first time the Variant was seen) to visibility (the scan found the file).
        pairs = connection.execute(
            sa.select(record.c.exported_at, catalog.unit_variant.c.first_seen_at)
            .select_from(
                record.join(export.export_item, export.export_item.c.id == record.c.export_item_id)
                .join(storage.content, storage.content.c.id == export.export_item.c.content_id)
                .join(
                    catalog.unit_variant, catalog.unit_variant.c.id == storage.content.c.variant_id
                )
            )
            .where(record.c.state == VISIBLE_STATE, record.c.exported_at >= since)
        ).all()
        delays = [(seen - first).total_seconds() for seen, first in pairs if seen and first]
        return Stats(
            since_days=since_days,
            human_interventions={
                "waiting_states": _events(connection, _WAITING_EVENTS, since),
                "needs_review_replays": int(replays),
            },
            units_exported=int(exported),
            median_detection_to_visible_s=(round(statistics.median(delays)) if delays else None),
            guards_triggered={
                "sentinel": _events(connection, SENTINEL_EVENTS, since),
                "foreign_writer": _events(connection, FOREIGN_WRITER_EVENTS, since),
            },
        )
