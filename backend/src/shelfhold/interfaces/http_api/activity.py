"""`/api/v1/activity`: events and download requests as one person may see them (EF-USR-016, D19).

Every item goes through `project_event` (`activity_view` plus the visibility filter of L09: an
item about a Work a child may not see leaves no trace); the route reads the row, resolves the
title of what it points at, and hands both over. An item whose title is masked says so
(`title_masked`).
Unmasking is its own call, behind the reserved action `request.unmask` (a session of an admin,
never a key): the right is checked before anything is read, so a refusal says the same thing
whether the entry exists or not; for the one who holds the right, an entry that is absent, not
visible to them or not masked is the same 404. The title is read, and audited, in one transaction.
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import Connection, Engine

from shelfhold.events.activity import (
    ActivityEvent,
    UnmaskRefusedError,
    Viewer,
    download_request_event,
    unmask_title,
)
from shelfhold.events.projection import project_event
from shelfhold.identity.public import (
    INTEGRATION_ROLE,
    RESERVED_ACTIONS,
    Credential,
    Principal,
    reader_scope,
)
from shelfhold.infrastructure.schema import acquisition, catalog, identity, platform
from shelfhold.interfaces.http_api.auth import gate_of, require_principal, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_activity_router"]

MAX_LIMIT = 100
_UNMASK = RESERVED_ACTIONS["request.unmask"]


class ActivityOut(BaseModel):
    event_id: int | str
    type: str
    visibility: str
    payload: dict[str, Any]
    occurred_at: datetime
    title: str | None
    title_masked: bool


class UnmaskedOut(BaseModel):
    event_id: int | str
    title: str


def viewer_of(principal: Principal, scope_kind: str = "all") -> Viewer:
    """A session reads as its profile. A key or a token of an adult is a program: it reads like
    the integration role. One of a child never reads more than the child does: it is the child
    (`scope_kind` is the one of her profile, read at the request), never the integration role,
    which sees every `ops` event and every sibling's `household` entry."""
    if principal.credential is Credential.SESSION:
        return Viewer(principal.user_id, principal.role)
    if scope_kind != "all":  # a child, a profile that is gone, a role nobody knows
        return Viewer(principal.user_id, "child")
    return Viewer(None, INTEGRATION_ROLE)


def _title_of(connection: Connection, payload: Mapping[str, object]) -> str | None:
    """The title the ULIDs of the payload point at, resolved at display time (never stored)."""
    for key, table in (("work_id", catalog.work), ("series_id", catalog.series)):
        ulid = payload.get(key)
        if isinstance(ulid, str):
            title = connection.execute(sa.select(table.c.title).where(table.c.id == ulid)).scalar()
            if title is not None:
                return str(title)
    return None


def _request_edition(connection: Connection, row: Any) -> str | None:
    """The Edition a request is about: through its Variant, or through the range it asks."""
    edition_id = (row.range_ref or {}).get("edition_id") if row.range_ref else None
    if row.variant_id is not None:
        edition_id = connection.execute(
            sa.select(catalog.unit.c.edition_id)
            .join(catalog.unit_variant, catalog.unit_variant.c.unit_id == catalog.unit.c.id)
            .where(catalog.unit_variant.c.id == row.variant_id)
        ).scalar()
    return edition_id if isinstance(edition_id, str) else None


def _request_title(connection: Connection, row: Any) -> str | None:
    """The Work a request is about, resolved through its Edition."""
    edition_id = _request_edition(connection, row)
    if edition_id is None:
        return None
    return _title_of(
        connection,
        {
            "work_id": connection.execute(
                sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == edition_id)
            ).scalar()
        },
    )


def _owner_role(connection: Connection, user_id: str | None) -> tuple[str | None, str | None]:
    if user_id is None:
        return None, None
    role = connection.execute(
        sa.select(identity.user.c.role).where(identity.user.c.id == user_id)
    ).scalar()
    return (user_id, str(role)) if role is not None else (None, None)


def _activity_event(connection: Connection, row: Any) -> ActivityEvent:
    owner_id, owner_role = _owner_role(connection, row.actor_id)
    return ActivityEvent(
        id=row.id, type=row.type, visibility=row.visibility, payload=row.payload or {},
        owner_id=owner_id, owner_role=owner_role, title=_title_of(connection, row.payload or {}),
    )  # fmt: skip


def _request_event(connection: Connection, row: Any) -> ActivityEvent:
    owner_id, owner_role = _owner_role(connection, row.requester_user_id)
    return download_request_event(
        request_id=row.id, state=row.state, visibility=row.visibility, requester_id=owner_id,
        requester_role=owner_role, title=_request_title(connection, row),
    )  # fmt: skip


def _out(item: Any, occurred_at: datetime) -> ActivityOut:
    return ActivityOut(
        event_id=item.event_id,
        type=item.type,
        visibility=item.visibility,
        payload=dict(item.payload),
        occurred_at=occurred_at,
        title=item.title,
        title_masked=item.title_masked,
    )


def build_activity_router(get_engine: Callable[[], Engine]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/activity", tags=["activity"])

    @router.get("")
    def list_activity(
        principal: Annotated[Principal, Depends(require_principal)],
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    ) -> list[ActivityOut]:
        found: list[ActivityOut] = []
        with get_engine().connect() as connection:
            # the reader's own profile, a key or a token included: never who issued the key
            scope = reader_scope(connection, principal.user_id)
            viewer = viewer_of(principal, scope.kind)
            events = connection.execute(
                sa.select(platform.event).order_by(platform.event.c.id.desc()).limit(limit * 4)
            )
            for row in events:  # the projection decides: a hidden entry leaves no trace
                item = project_event(connection, viewer, scope, _activity_event(connection, row))
                if item is not None:
                    found.append(_out(item, row.occurred_at))
            requests = connection.execute(
                sa.select(acquisition.download_request)
                .order_by(acquisition.download_request.c.requested_at.desc())
                .limit(limit * 4)
            )
            for request in requests:
                item = project_event(
                    connection, viewer, scope, _request_event(connection, request),
                    editions=[e] if (e := _request_edition(connection, request)) else [],
                )  # fmt: skip
                if item is not None:
                    found.append(_out(item, request.requested_at))
        found.sort(key=lambda entry: entry.occurred_at, reverse=True)
        return found[:limit]

    @router.post("/{ref}/unmask")
    def unmask(
        ref: str,
        request: Request,
        principal: Annotated[Principal, Depends(reserved_action(_UNMASK, audit=False))],
    ) -> UnmaskedOut:
        """`ref`: the number of an event, or the ULID of a download request."""
        auth = gate_of(request).auth
        with get_engine().begin() as connection:
            entry = _entry(connection, ref)
            if entry is None:
                raise ProblemError("not_found")
            try:
                item = unmask_title(
                    connection, principal, entry, now=auth.now(),
                    fresh_auth_s=auth.fresh_auth_s, hardened=auth.hardened,
                    restricted_profile_exists=auth.restricted_profile_exists(),
                )  # fmt: skip
            except UnmaskRefusedError:
                raise ProblemError("not_found") from None  # nothing masked for this viewer
        if item.title is None:  # pragma: no cover (`unmask_title` returns a title or raises)
            raise ProblemError("not_found")
        return UnmaskedOut(event_id=item.event_id, title=item.title)

    return router


def _entry(connection: Connection, ref: str) -> ActivityEvent | None:
    if ref.isascii() and ref.isdigit():
        row = connection.execute(
            sa.select(platform.event).where(platform.event.c.id == int(ref))
        ).first()
        return _activity_event(connection, row) if row is not None else None
    request = connection.execute(
        sa.select(acquisition.download_request).where(acquisition.download_request.c.id == ref)
    ).first()
    return _request_event(connection, request) if request is not None else None
