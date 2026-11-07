"""`activity_view(viewer, event)`: the one projection of an event for a person (EF-USR-016, D19).

Every surface that shows an event (the activity page, a notification, a webhook or an outbox
payload) goes through here. An event row carries identifiers and codes only (see `bus`); the
title of what it is about is resolved by the caller at display time and handed in as
`ActivityEvent.title`. This module is the only place of `events` where an event meets a title, and
a test holds that line.

The rules, per viewer role and `visibility` (anything not listed is hidden: closed by default):

- `ops`: an operation (job, bytes, error). Admin sees it with its title; the integration role
  sees it without one; members and children do not see it.
- `household`: visible to the adults of the house (admin, member) and, for their own items, to
  the owner. A child sees only their own. The integration role gets the operation, never a title.
- `private`: the owner sees it. Admin sees the operation of another adult's request with the
  **title masked**, and the title of a child's (parental oversight). Nobody else sees it.
  The integration role never receives a private event (a private title never leaves).

Unmasking is explicit (`unmask_title`): a reserved action (session or host, admin, never a key),
audited in the caller's transaction. Without a request model before M3 (D19), the requester is an
optional owner on the event.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Connection

from shelfhold.identity.public import (
    INTEGRATION_ROLE,
    RESERVED_ACTIONS,
    Allowed,
    AuditEntry,
    Principal,
    Verdict,
    audit_actor,
    audit_insert,
    authorize_reserved,
)

ADULT_ROLES = frozenset({"admin", "member"})
VISIBILITIES = frozenset({"private", "household", "ops"})
_UNMASK = RESERVED_ACTIONS["request.unmask"]


@dataclass(frozen=True)
class Viewer:
    user_id: str | None
    role: str


@dataclass(frozen=True)
class ActivityEvent:
    """An event as the projection sees it. `title` is the display text resolved from the ULIDs
    of the payload by the caller; `owner_*` is the person it belongs to, when there is one."""

    id: int
    type: str
    visibility: str
    payload: Mapping[str, object]
    owner_id: str | None = None
    owner_role: str | None = None
    title: str | None = field(default=None, repr=False)  # never in a log line


@dataclass(frozen=True)
class ActivityItem:
    event_id: int
    type: str
    visibility: str
    payload: Mapping[str, object]
    title: str | None = field(default=None, repr=False)
    title_masked: bool = False


def _item(event: ActivityEvent, *, title: str | None, masked: bool) -> ActivityItem:
    return ActivityItem(event.id, event.type, event.visibility, event.payload, title, masked)


def _project(viewer: Viewer, event: ActivityEvent, *, reveal: bool = False) -> ActivityItem | None:
    visibility = event.visibility
    if visibility not in VISIBILITIES:
        return None
    role = viewer.role
    own = viewer.user_id is not None and viewer.user_id == event.owner_id
    if role == INTEGRATION_ROLE:  # webhooks and outbox (GAP-7-16): never a title, never private
        return None if visibility == "private" else _item(event, title=None, masked=False)
    if role == "admin":
        if visibility != "private" or own or event.owner_role == "child":
            return _item(event, title=event.title, masked=False)
        if reveal:
            return _item(event, title=event.title, masked=False)
        return _item(event, title=None, masked=True)
    if role == "member":
        if visibility == "household" and (own or event.owner_role != "child"):
            return _item(event, title=event.title, masked=False)
        if visibility == "private" and own:
            return _item(event, title=event.title, masked=False)
        return None
    if role == "child":
        if visibility != "ops" and own:
            return _item(event, title=event.title, masked=False)
        return None
    return None  # a role nobody declared sees nothing


def activity_view(viewer: Viewer, event: ActivityEvent) -> ActivityItem | None:
    """What `viewer` may see of `event`, or None when the event is not theirs to see."""
    return _project(viewer, event)


class UnmaskRefusedError(PermissionError):
    """The reserved-action rule refused the unmasking, or there was nothing masked to reveal."""

    def __init__(self, verdict: Verdict | None = None) -> None:
        name = type(verdict).__name__ if verdict is not None else "NothingMasked"
        super().__init__(f"request.unmask refused: {name}")
        self.verdict = verdict


def unmask_title(
    connection: Connection,
    principal: Principal,
    event: ActivityEvent,
    *,
    now: datetime,
    fresh_auth_s: int = 900,
    hardened: bool = False,
    restricted_profile_exists: bool = False,
) -> ActivityItem:
    """Reveal the masked title of `event` to `principal`, and write the audit line on
    `connection`: the title is returned only if the caller's transaction can commit the entry, and
    a rollback takes both. The settings (`fresh_auth_s`, `hardened`) come from the caller's
    configuration, never from a request."""
    verdict = authorize_reserved(
        principal, _UNMASK, now=now, fresh_auth_s=fresh_auth_s, hardened=hardened,
        restricted_profile_exists=restricted_profile_exists,
    )  # fmt: skip
    if not isinstance(verdict, Allowed):
        raise UnmaskRefusedError(verdict)
    viewer = Viewer(principal.user_id, principal.role)
    masked = _project(viewer, event)
    if masked is None or not masked.title_masked or event.title is None:
        raise UnmaskRefusedError  # only what is masked for this viewer can be revealed
    connection.execute(
        audit_insert(
            AuditEntry(
                actor_id=audit_actor(principal),
                action=_UNMASK.name,
                at=now,
                target=f"event:{event.id}",
                session_id=principal.session_id,
                auth_time=principal.auth_time,
            )
        )
    )
    return _item(event, title=event.title, masked=False)
