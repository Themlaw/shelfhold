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

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import Engine

from shelfhold.events.activity import (
    UnmaskRefusedError,
    Viewer,
)
from shelfhold.events.family_state import (
    FamilyState,
    download_family_state,
    event_family_state,
)
from shelfhold.events.projection import project_event
from shelfhold.identity.public import (
    INTEGRATION_ROLE,
    RESERVED_ACTIONS,
    Credential,
    Principal,
    reader_scope,
)
from shelfhold.interfaces.http_api.auth import gate_of, require_principal, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops.activity_feed import (
    activity_event_of,
    kavita_open_url,
    recent_events,
    recent_requests,
    request_edition,
    request_event,
    unmask_entry,
)

__all__ = ["activity_event_of", "build_activity_router", "family_payload", "viewer_of"]

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
    family_state: FamilyState | None  # the one word the family reads (EF-UX-010)
    open_url: str | None  # where "Ouvrir dans Kavita" goes: only for an item that is available
    koreader_reset: bool  # the admin's: KOReader's position was reset (EF-EXP-016)


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


def family_payload(role: str, event_type: str, payload: Mapping[str, object]) -> dict[str, Any]:
    """What of the payload a viewer reads. The steps and `koreader_reset` are the admin's: the
    family reads `family_state`, so a download's step is dropped and the flag never leaves."""
    if role == "admin":
        return dict(payload)
    if event_type == "download_request":
        return {}
    return {k: v for k, v in payload.items() if k != "koreader_reset"}


def _family_state(item: Any) -> FamilyState | None:
    if item.type == "download_request":
        return download_family_state(str(item.payload.get("state", "")))
    return event_family_state(item.type)


def _out(
    item: Any, occurred_at: datetime, role: str, kavita: Callable[[], str | None]
) -> ActivityOut:
    state = _family_state(item)
    return ActivityOut(
        event_id=item.event_id,
        type=item.type,
        visibility=item.visibility,
        payload=family_payload(role, item.type, item.payload),
        occurred_at=occurred_at,
        title=item.title,
        title_masked=item.title_masked,
        family_state=state,
        # a key reads like the integration role: it is handed no address of the household
        open_url=kavita() if state == "available" and role != INTEGRATION_ROLE else None,
        koreader_reset=role == "admin" and bool(item.payload.get("koreader_reset")),
    )  # fmt: skip


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
            address: list[str | None] = []

            def kavita() -> str | None:  # read once, and only if an item is available
                if not address:
                    address.append(kavita_open_url(connection))
                return address[0]

            events = recent_events(connection, limit * 4)
            for row in events:  # the projection decides: a hidden entry leaves no trace
                item = project_event(connection, viewer, scope, activity_event_of(connection, row))
                if item is not None:
                    found.append(_out(item, row.occurred_at, viewer.role, kavita))
            for request in recent_requests(connection, limit * 4):
                item = project_event(
                    connection, viewer, scope, request_event(connection, request),
                    editions=[e] if (e := request_edition(connection, request)) else [],
                )  # fmt: skip
                if item is not None:
                    found.append(_out(item, request.requested_at, viewer.role, kavita))
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
        try:
            item = unmask_entry(
                get_engine(), principal, ref, now=auth.now(),
                fresh_auth_s=auth.fresh_auth_s, hardened=auth.hardened,
                restricted_profile_exists=auth.restricted_profile_exists(),
            )  # fmt: skip
        except UnmaskRefusedError:
            raise ProblemError("not_found") from None  # nothing masked for this viewer
        if item is None:
            raise ProblemError("not_found")
        if item.title is None:  # pragma: no cover (`unmask_title` returns a title or raises)
            raise ProblemError("not_found")
        return UnmaskedOut(event_id=item.event_id, title=item.title)

    return router
