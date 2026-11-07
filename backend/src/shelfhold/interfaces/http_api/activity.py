"""`/api/v1/activity`: the events as one person may see them (EF-USR-016, D19).

Every item goes through `activity_view`; the route reads the row, resolves the title of what the
payload points at, and hands both over. An item whose title is masked says so (`title_masked`).
Unmasking is its own call: a reserved action (session of an admin, never a key), audited in the
transaction that reads the title.
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
    activity_view,
    unmask_title,
)
from shelfhold.identity.public import (
    INTEGRATION_ROLE,
    RESERVED_ACTIONS,
    Credential,
    Principal,
)
from shelfhold.infrastructure.schema import catalog, identity, platform
from shelfhold.interfaces.http_api.auth import gate_of, require_principal
from shelfhold.interfaces.http_api.idempotency import NEVER_STORE
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_activity_router"]

MAX_LIMIT = 100


class ActivityOut(BaseModel):
    event_id: int
    type: str
    visibility: str
    payload: dict[str, Any]
    occurred_at: datetime
    title: str | None
    title_masked: bool


class UnmaskedOut(BaseModel):
    event_id: int
    title: str


def viewer_of(principal: Principal) -> Viewer:
    """A key or a token is a program: it reads like the integration role, whoever issued it."""
    if principal.credential in (Credential.SESSION, Credential.LOCAL_CLI):
        return Viewer(principal.user_id, principal.role)
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


def _owner_role(connection: Connection, actor_id: str | None) -> tuple[str | None, str | None]:
    if actor_id is None:
        return None, None
    role = connection.execute(
        sa.select(identity.user.c.role).where(identity.user.c.id == actor_id)
    ).scalar()
    return (actor_id, str(role)) if role is not None else (None, None)


def _activity_event(connection: Connection, row: Any) -> ActivityEvent:
    owner_id, owner_role = _owner_role(connection, row.actor_id)
    return ActivityEvent(
        id=row.id, type=row.type, visibility=row.visibility, payload=row.payload or {},
        owner_id=owner_id, owner_role=owner_role, title=_title_of(connection, row.payload or {}),
    )  # fmt: skip


def _unmask_principal(request: Request) -> Principal:
    """The caller of an unmasking. The answer is never kept for replay. No audit line is written
    here: `unmask_title` writes its own, with the event, in the transaction that reads the title."""
    request.scope.setdefault("state", {})[NEVER_STORE] = True
    return require_principal(request)


def build_activity_router(get_engine: Callable[[], Engine]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/activity", tags=["activity"])

    @router.get("")
    def list_activity(
        principal: Annotated[Principal, Depends(require_principal)],
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    ) -> list[ActivityOut]:
        viewer = viewer_of(principal)
        out: list[ActivityOut] = []
        with get_engine().connect() as connection:
            rows = connection.execute(
                sa.select(platform.event).order_by(platform.event.c.id.desc()).limit(limit * 4)
            )
            for row in rows:  # the projection decides: a hidden event leaves no trace
                item = activity_view(viewer, _activity_event(connection, row))
                if item is None:
                    continue
                out.append(
                    ActivityOut(
                        event_id=item.event_id,
                        type=item.type,
                        visibility=item.visibility,
                        payload=dict(item.payload),
                        occurred_at=row.occurred_at,
                        title=item.title,
                        title_masked=item.title_masked,
                    )
                )
                if len(out) == limit:
                    break
        return out

    @router.post("/{event_id}/unmask")
    def unmask(
        event_id: int,
        request: Request,
        principal: Annotated[Principal, Depends(_unmask_principal)],
    ) -> UnmaskedOut:
        auth = gate_of(request).auth
        with get_engine().begin() as connection:
            row = connection.execute(
                sa.select(platform.event).where(platform.event.c.id == event_id)
            ).first()
            if row is None:  # only an administrator is told; for anyone else it is a refusal
                raise ProblemError("not_found" if principal.role == "admin" else "forbidden")
            try:
                item = unmask_title(
                    connection, principal, _activity_event(connection, row), now=auth.now(),
                    fresh_auth_s=auth.fresh_auth_s, hardened=auth.hardened,
                    restricted_profile_exists=auth.restricted_profile_exists(),
                )  # fmt: skip
            except UnmaskRefusedError as refusal:
                raise _problem_of(refusal) from None
        assert item.title is not None  # noqa: S101 (`unmask_title` returns a title or raises)
        return UnmaskedOut(event_id=item.event_id, title=item.title)

    return router


def _problem_of(refusal: UnmaskRefusedError) -> ProblemError:
    from shelfhold.identity.public import CredentialNotAllowed, ReauthRequired

    verdict = refusal.verdict
    extensions = {"action": RESERVED_ACTIONS["request.unmask"].name}
    if isinstance(verdict, CredentialNotAllowed):
        return ProblemError("credential_not_allowed", extensions=extensions)
    if isinstance(verdict, ReauthRequired):
        return ProblemError(
            "reauth_required",
            extensions={
                **extensions, "auth_age_s": verdict.auth_age_s, "max_age_s": verdict.max_age_s,
            },
        )  # fmt: skip
    return ProblemError("forbidden", extensions=extensions)  # a role, or nothing masked
