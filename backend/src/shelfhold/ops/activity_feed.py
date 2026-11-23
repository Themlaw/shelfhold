"""What the activity screen reads (EF-USR-016, D19, M3 L41): the rows of the events and of the
download requests, the owner and the title behind each, the one entry an unmask names, and the
addresses « Ouvrir dans Kavita » (and every other kind of destination, J5-S2) open. The HTTP route
holds none of these queries (ENF-QUAL-002): it asks here, hands each row to `project_event` (the
filter of L09) and words the result.

The addresses are the ones of the targets the admin connected (web addresses of the local
network, nothing secret), read here and never from the client.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.events.activity import (
    ActivityEvent,
    ActivityItem,
    download_request_event,
    unmask_title,
)
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import acquisition, catalog, export, identity, platform

__all__ = [
    "activity_event_of",
    "entry_of",
    "kavita_open_url",
    "kavita_open_url_of",
    "open_urls",
    "open_urls_of",
    "recent_events",
    "recent_requests",
    "request_edition",
    "request_event",
    "unmask_entry",
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


def open_urls(connection: Connection) -> dict[str, str]:
    """The address of the first target of each kind that has one (a web address of the local
    network, nothing secret), read here and never from the client: what « Ouvrir dans <kind> »
    opens, whatever the kind of the destination (J5-S2)."""
    found: dict[str, str] = {}
    rows = connection.execute(
        sa.select(export.export_target.c.kind, export.export_target.c.config).order_by(
            export.export_target.c.kind, export.export_target.c.id
        )
    )
    for kind, config in rows:
        url = config.get("url") if isinstance(config, dict) else None
        if isinstance(url, str) and url.startswith(_SCHEMES) and str(kind) not in found:
            found[str(kind)] = url
    return found


def open_urls_of(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        return open_urls(connection)


def kavita_open_url(connection: Connection) -> str | None:
    """The first Kavita target's address, or None (nothing is connected: no link is invented)."""
    return open_urls(connection).get("kavita")


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
    refs = _arrival_refs(connection, body) if row.type == "ops.request_arrived" else {}
    owner_id, owner_role = owner_and_role(connection, row.actor_id)
    return ActivityEvent(
        id=row.id, type=row.type, visibility=row.visibility, payload=body,
        owner_id=owner_id, owner_role=owner_role,
        title=title_of(connection, body),  # the title of the event as stored, not of the refs
        refs=refs,
    )  # fmt: skip


def _arrival_refs(connection: Connection, body: Mapping[str, object]) -> dict[str, object]:
    """`ops.request_arrived` stores the request alone. The Work and the Edition it arrived as are
    read now and handed to the projection, so the one filter of L09 hides the event from a reader
    who may not see them (M3 closure M1); the served payload stays the stored one. A request that
    cannot be found names a Work that is not found: hidden from a child, like any absent thing."""
    found = connection.execute(
        sa.select(identity.request.c.work_id, identity.request.c.edition_id).where(
            identity.request.c.id == body.get("request_id")
        )
    ).first()
    refs: dict[str, object] = {"work_id": found.work_id if found and found.work_id else ""}
    if found is not None and found.edition_id:
        refs["edition_id"] = found.edition_id
    return refs


def request_event(connection: Connection, row: Any) -> ActivityEvent:
    owner_id, owner_role = owner_and_role(connection, row.requester_user_id)
    return download_request_event(
        request_id=row.id, state=row.state, visibility=row.visibility, requester_id=owner_id,
        requester_role=owner_role, title=request_title(connection, row),
    )  # fmt: skip


def unmask_entry(
    engine: Engine,
    principal: Principal,
    ref: str,
    *,
    now: datetime,
    fresh_auth_s: int,
    hardened: bool,
    restricted_profile_exists: bool,
) -> ActivityItem | None:
    """The title behind `ref`, and its audit line written in the same transaction (that takes the
    write lock first: it reads the entry, then writes). `None`: no such entry. A refusal
    (`UnmaskRefusedError`) leaves the transaction, and so the audit line, rolled back."""
    with write_transaction(engine) as connection:
        entry = entry_of(connection, ref)
        if entry is None:
            return None
        return unmask_title(
            connection, principal, entry, now=now, fresh_auth_s=fresh_auth_s,
            hardened=hardened, restricted_profile_exists=restricted_profile_exists,
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
