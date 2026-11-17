"""The events a program outside the house may read, in order (EF-INT-005, EF-INT-002).

The outbox of the webhooks reads the `event` table through this module and no other door: each row
is brought to the current version of its shape (`upcast_payload`), then handed to `project_event`
with the viewer of the integration role, so what comes out is what `activity_view` lets a program
see: nothing private, no title. The module sits in `events` because the table does; the webhooks
package may not import the table, the bus or the stream (an import contract holds that line).

A row nobody can read (a version of a later release, a payload the steps cannot bring up) is
skipped and counted in the log by its type, never raised: one bad row must not stop the others.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection

from shelfhold.events.activity import ActivityEvent, Viewer
from shelfhold.events.bus import SCHEMA_VERSION
from shelfhold.events.projection import project_event
from shelfhold.events.upcast import UnreadableVersionError, Upcaster, upcast_payload
from shelfhold.identity.public import INTEGRATION_ROLE, reader_scope
from shelfhold.infrastructure.schema import identity, platform

__all__ = ["RETAINED_VERSIONS", "FeedEvent", "last_event_id", "read_after", "read_one"]

log = structlog.get_logger(__name__)

# The versions a stored row may carry and the reader still presents: every one since the first.
RETAINED_VERSIONS: Final = tuple(range(1, SCHEMA_VERSION + 1))
_VIEWER: Final = Viewer(None, INTEGRATION_ROLE)


@dataclass(frozen=True)
class FeedEvent:
    event_id: int
    type: str
    occurred_at: datetime
    payload: Mapping[str, Any]  # at the current version, as the integration role sees it
    stored_version: int  # the version of the row: kept as written, never rewritten


def last_event_id(connection: Connection) -> int:
    """The newest event so far (0 for none): where a new subscription starts."""
    found = connection.execute(sa.select(sa.func.max(platform.event.c.id))).scalar()
    return int(found or 0)


def read_after(
    connection: Connection,
    after_id: int,
    *,
    as_user: str | None,
    limit: int = 200,
    current: int = SCHEMA_VERSION,
    steps: Mapping[tuple[str, int], Upcaster] | None = None,
    require_admin: bool = False,
) -> tuple[list[FeedEvent], int]:
    """The events after `after_id` that `as_user`'s subscription may carry out, and the cursor to
    resume from: the id of the last row examined, shown or not.

    `as_user` is the profile the payload is read through; a profile that is gone reads as
    nobody, who sees nothing. With `require_admin`, a profile that is not an administrator any
    more gives nothing and does not move the cursor: a webhook is an administrator's."""
    if require_admin and not _is_admin(connection, as_user):
        return [], after_id
    table = platform.event
    rows = connection.execute(
        sa.select(table).where(table.c.id > after_id).order_by(table.c.id).limit(limit)
    ).all()
    scope = reader_scope(connection, as_user)
    shown: list[FeedEvent] = []
    cursor = after_id
    for row in rows:
        cursor = int(row.id)
        try:
            payload = upcast_payload(
                row.type, row.schema_version, row.payload, current=current, steps=steps
            )
        except (UnreadableVersionError, KeyError, TypeError, ValueError):
            log.warning("webhook.event_unreadable", event_type=row.type)
            continue
        event = ActivityEvent(
            id=cursor, type=row.type, visibility=row.visibility, payload=payload,
            owner_id=row.actor_id,
        )  # fmt: skip
        item = project_event(connection, _VIEWER, scope, event)
        if item is not None:
            shown.append(
                FeedEvent(cursor, row.type, row.occurred_at, dict(item.payload), row.schema_version)
            )
    return shown, cursor


def _is_admin(connection: Connection, user_id: str | None) -> bool:
    if not user_id:
        return False
    role = connection.execute(
        sa.select(identity.user.c.role).where(identity.user.c.id == user_id)
    ).scalar()
    return role == "admin"


def read_one(
    connection: Connection,
    event_id: int,
    *,
    as_user: str | None,
    current: int = SCHEMA_VERSION,
    steps: Mapping[tuple[str, int], Upcaster] | None = None,
    require_admin: bool = False,
) -> FeedEvent | None:
    """One event as `as_user`'s subscription may carry it, read again at the moment of a delivery
    (never kept): None when it is gone (the 90-day purge) or no longer theirs to send."""
    shown, _cursor = read_after(
        connection, event_id - 1, as_user=as_user, limit=1, current=current, steps=steps,
        require_admin=require_admin,
    )  # fmt: skip
    return shown[0] if shown and shown[0].event_id == event_id else None
