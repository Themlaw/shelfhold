"""What the activity screen reads (EF-USR-016, D19, M3 L41): the rows of the events and of the
download requests, the owner and the title behind each, the one entry an unmask names, and the
address that "Ouvrir dans Kavita" opens. The HTTP route holds none of these queries (ENF-QUAL-002):
it asks here, hands each row to `project_event` (the filter of L09) and words the result.

The address is the one of the Kavita target the admin connected (a web address of the local
network, nothing secret), read here and never from the client.
"""

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.events.activity import ActivityEvent, download_request_event
from shelfhold.infrastructure.schema import acquisition, catalog, export, identity, platform

__all__ = [
    "activity_event_of",
    "entry_of",
    "kavita_open_url",
    "kavita_open_url_of",
    "recent_events",
    "recent_requests",
    "request_edition",
    "request_event",
]

_SCHEMES = ("http://", "https://")


def recent_events(connection: Connection, limit: int) -> list[Any]:
    """The newest event rows, before the projection decides which ones are the reader's."""
    return list(
        connection.execute(
            sa.select(platform.event).order_by(platform.event.c.id.desc()).limit(limit)
        )
    )


def recent_requests(connection: Connection, limit: int) -> list[Any]:
    request = acquisition.download_request
    return list(
        connection.execute(sa.select(request).order_by(request.c.requested_at.desc()).limit(limit))
    )


def kavita_open_url(connection: Connection) -> str | None:
    """The first Kavita target's address, or None (nothing is connected: no link is invented)."""
    config = connection.execute(
        sa.select(export.export_target.c.config)
        .where(export.export_target.c.kind == "kavita")
        .order_by(export.export_target.c.id)
        .limit(1)
    ).scalar()
    url = config.get("url") if isinstance(config, dict) else None
    return url if isinstance(url, str) and url.startswith(_SCHEMES) else None


def kavita_open_url_of(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return kavita_open_url(connection)


def title_of(connection: Connection, payload: Mapping[str, object]) -> str | None:
    """The title the ULIDs of the payload point at, resolved at display time (never stored)."""
    for key, table in (("work_id", catalog.work), ("series_id", catalog.series)):
        ulid = payload.get(key)
        if isinstance(ulid, str):
            title = connection.execute(sa.select(table.c.title).where(table.c.id == ulid)).scalar()
            if title is not None:
                return str(title)
    return None


def request_edition(connection: Connection, row: Any) -> str | None:
    """The Edition a request is about: through its Variant, or through the range it asks."""
    edition_id = (row.range_ref or {}).get("edition_id") if row.range_ref else None
    if row.variant_id is not None:
        edition_id = connection.execute(
            sa.select(catalog.unit.c.edition_id)
            .join(catalog.unit_variant, catalog.unit_variant.c.unit_id == catalog.unit.c.id)
            .where(catalog.unit_variant.c.id == row.variant_id)
        ).scalar()
    return edition_id if isinstance(edition_id, str) else None


def request_title(connection: Connection, row: Any) -> str | None:
    """The Work a request is about, resolved through its Edition."""
    edition_id = request_edition(connection, row)
    if edition_id is None:
        return None
    return title_of(
        connection,
        {
            "work_id": connection.execute(
                sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == edition_id)
            ).scalar()
        },
    )


def owner_and_role(connection: Connection, user_id: str | None) -> tuple[str | None, str | None]:
    if user_id is None:
        return None, None
    role = connection.execute(
        sa.select(identity.user.c.role).where(identity.user.c.id == user_id)
    ).scalar()
    return (user_id, str(role)) if role is not None else (None, None)


def activity_event_of(
    connection: Connection, row: Any, payload: Mapping[str, object] | None = None
) -> ActivityEvent:
    """`payload`: the row's own, unless a reader of an older row has moved it to the current
    version (the SSE stream)."""
    body = row.payload or {} if payload is None else payload
    owner_id, owner_role = owner_and_role(connection, row.actor_id)
    return ActivityEvent(
        id=row.id, type=row.type, visibility=row.visibility, payload=body,
        owner_id=owner_id, owner_role=owner_role, title=title_of(connection, body),
    )  # fmt: skip


def request_event(connection: Connection, row: Any) -> ActivityEvent:
    owner_id, owner_role = owner_and_role(connection, row.requester_user_id)
    return download_request_event(
        request_id=row.id, state=row.state, visibility=row.visibility, requester_id=owner_id,
        requester_role=owner_role, title=request_title(connection, row),
    )  # fmt: skip


def entry_of(connection: Connection, ref: str) -> ActivityEvent | None:
    if ref.isascii() and ref.isdigit():
        row = connection.execute(
            sa.select(platform.event).where(platform.event.c.id == int(ref))
        ).first()
        return activity_event_of(connection, row) if row is not None else None
    request = connection.execute(
        sa.select(acquisition.download_request).where(acquisition.download_request.c.id == ref)
    ).first()
    return request_event(connection, request) if request is not None else None
