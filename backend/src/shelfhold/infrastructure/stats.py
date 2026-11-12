"""Local value indicators (EF-INT-025): computed on the instance, from existing tables only.

`stats --since 30d` and `GET /api/v1/stats` read the same figures. Nothing is stored for them
(no view, no table: the arrival time is `export_record.resolved_at`, set by the arrival polling)
and nothing leaves the instance: the module opens no connection but the database's
(ENF-PRIV-001). The window is the last `since_days` days up to `now`.
"""

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.events.operating import WAITING_STATE_EVENT
from shelfhold.infrastructure.schema import (
    acquisition,
    catalog,
    export,
    identity,
    platform,
    storage,
)

MAX_SINCE_DAYS = 3650
DEFAULT_SINCE = "30d"
_SINCE = re.compile(r"^([1-9][0-9]{0,3})d$")

# Rows that count as an exported Unit, and the one that means "visible in Kavita": a scan found it.
EXPORTED_STATES = ("written", "scanned", "resolved")
VISIBLE_STATE = "resolved"
AWAITED_STATES = ("written", "scanned")  # waiting for the arrival polling to see them
SENTINEL_EVENTS = ("mount.sentinel_missing",)
FOREIGN_WRITER_EVENTS = ("target.foreign_writer_detected",)
DELIVERED_REQUESTS = ("downloaded", "fetched")  # the file came back from the source
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
    arrivals_pending: int  # written, not yet seen in the destination's index
    guards_triggered: dict[str, int]
    # M3: who asked for what, and the share that came back (EF-INT-025); never a requester that
    # is no profile (an operation of the administrator has none)
    requests_by_profile: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _events(connection: sa.Connection, types: tuple[str, ...], since: datetime) -> int:
    event = platform.event
    return int(
        connection.execute(
            sa.select(sa.func.count()).where(event.c.type.in_(types), event.c.occurred_at >= since)
        ).scalar_one()
    )


def _median_delay(connection: sa.Connection, since: datetime) -> int | None:
    """Detection (first time the Variant was seen) to visibility (`resolved_at`: the arrival
    polling found the file in Kavita), in seconds. The median is taken by the database: one or two
    rows come back, never the set."""
    record = export.export_record
    delay = sa.func.round(
        (
            sa.func.julianday(record.c.resolved_at)
            - sa.func.julianday(catalog.unit_variant.c.first_seen_at)
        )
        * 86400.0
    ).label("delay")
    joined = (
        sa.select(delay)
        .select_from(
            record.join(export.export_item, export.export_item.c.id == record.c.export_item_id)
            .join(storage.content, storage.content.c.id == export.export_item.c.content_id)
            .join(catalog.unit_variant, catalog.unit_variant.c.id == storage.content.c.variant_id)
        )
        .where(record.c.state == VISIBLE_STATE, record.c.resolved_at >= since)
        .subquery()
    )
    count = int(connection.execute(sa.select(sa.func.count()).select_from(joined)).scalar_one())
    if count == 0:
        return None
    middle = (
        connection.execute(
            sa.select(joined.c.delay)
            .order_by(joined.c.delay)
            .limit(2 - count % 2)
            .offset((count - 1) // 2)
        )
        .scalars()
        .all()
    )
    return round(float(sum(middle) / len(middle)))


def _requests_by_profile(connection: sa.Connection, since: datetime) -> list[dict[str, object]]:
    """Requests of the window per profile, with the delivered ones (`fetched`: the file is in the
    library) and their share in whole percent. Most requests first, then by name."""
    request, user = acquisition.download_request, identity.user
    delivered = sa.func.sum(sa.case((request.c.state.in_(DELIVERED_REQUESTS), 1), else_=0))
    rows = connection.execute(
        sa.select(user.c.id, user.c.username, sa.func.count().label("n"), delivered.label("ok"))
        .select_from(request.join(user, user.c.id == request.c.requester_user_id))
        .where(request.c.requested_at >= since)
        .group_by(user.c.id, user.c.username)
        .order_by(sa.func.count().desc(), user.c.username)
    )
    return [
        {
            "profile_id": r.id,
            "username": r.username,
            "requested": int(r.n),
            "delivered": int(r.ok or 0),
            "delivered_share_percent": round(100 * int(r.ok or 0) / int(r.n)),
        }
        for r in rows
    ]


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
        median = _median_delay(connection, since)
        pending = connection.execute(
            sa.select(sa.func.count()).where(record.c.state.in_(AWAITED_STATES))
        ).scalar_one()
        return Stats(
            since_days=since_days,
            human_interventions={
                "waiting_states": _events(connection, _WAITING_EVENTS, since),
                "needs_review_replays": int(replays),
            },
            units_exported=int(exported),
            median_detection_to_visible_s=median,
            arrivals_pending=int(pending),
            guards_triggered={
                "sentinel": _events(connection, SENTINEL_EVENTS, since),
                "foreign_writer": _events(connection, FOREIGN_WRITER_EVENTS, since),
            },
            requests_by_profile=_requests_by_profile(connection, since),
        )
