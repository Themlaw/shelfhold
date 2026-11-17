"""What left a list: the removals behind `?since=` (EF-INT-001, M3 L11), from the `event` table.

A list of Works is told to a reader by one filter (`queries.visible_works_clause`). Two things
make a Work leave a reader's list: it is deleted, or it stops fitting the reader's view (an admin
raises its age, a stricter Edition arrives). Both are written to the sequence of events, in the
transaction of the change:

- `ops.work_visibility` `{work_id, need_before, need_after, created_seq}`: the *need* of a Work is
  the lowest ceiling from which the filter shows it to a child (`NEED_NONE` when it shows it to no
  child:
  unrated, flagged adult, an open suggested age above its rating). A Work whose need changed is
  recorded; a write that changes nothing writes nothing;
- `ops.work_removed` `{work_id, need_before, created_seq}`, written before the row goes;
- `ops.shelf_removed` `{shelf_id}`: shelves have no age, only an admin reads them;
- `ops.profile_visibility` `{work_id, profile_id, visible_before, visible_after, created_seq}`
  (M4 L39, D-P3): what a shelf list or the shelf of an Edition changes is not an age, so it is
  written per child profile, by the filter itself (`visible_works_clause` of that profile), for
  each Work whose visibility to her moved. `removed_ids` reads, for a child, the events of her own
  profile besides the needs. Wrapped by `track_shelf_changes`.

The payload holds ids and numbers, never a title: a token reveals nothing about the object.

**Never an item the reader never saw.** A reader is told about a removal only if she could see the
Work *when her token was issued*. That moment is read from the events themselves: the first event
of a Work after the token carries the need the Work had at the token (every change of need is an
event, so nothing moves between two of them unrecorded). A Work raised and lowered again, rated
after the token and then deleted, or one a younger reader never saw, is therefore not in her
removals. The need is computed by the filter itself (`work_need` asks `age_visible_works_clause`:
the age part, which no shelf changes), so the removals and the pages can never disagree.

A Work created after the token is not in the removals either: its first event would carry a need
it never had at the token, so each event also carries `created_seq` (`work.created_seq`, set by a
trigger that moves the sequence by one) and the reader ignores a Work created after the token.

**Retention.** Events older than the retention are purged (ENF-OPS-009), and the purge records the
highest id it took (`instance.events_purged_through`). A token under it, or newer than the head (a
restored backup), cannot be answered: `window` gives both, and the route answers
`resync_required`. More than `MAX_REMOVED` removals do too: the
client reloads the list rather than receiving an unbounded answer.

A writer that moves the visibility of a Work outside the rating route wraps its write in
`track_work_changes`; one that is not wrapped leaves no event (see the decision record of L11).
"""

import hashlib
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import sqlalchemy as sa

from shelfhold.catalog.application.queries import age_visible_works_clause, visible_work_ids
from shelfhold.infrastructure.schema import catalog, identity, platform

__all__ = [
    "ADMIN_SCOPE",
    "EVENT_SCHEMA_VERSION",
    "MAX_REMOVED",
    "NEED_NONE",
    "TooManyRemovalsError",
    "Window",
    "history_marker",
    "record_removed",
    "record_work_removed",
    "removed_ids",
    "restore_epoch",
    "scope_code",
    "track_shelf_changes",
    "track_work_changes",
    "window",
    "work_need",
]

EVENT_SCHEMA_VERSION: Final = 1  # the one of `events.bus` (a test holds the two together)
NEED_NONE: Final = 100  # shown to no reader with a ceiling
MAX_REMOVED: int = 1000
VISIBILITY_EVENT: Final = "ops.work_visibility"
WORK_REMOVED_EVENT: Final = "ops.work_removed"
SHELF_REMOVED_EVENT: Final = "ops.shelf_removed"
PROFILE_EVENT: Final = "ops.profile_visibility"
_MAX_CEILING = 99  # X
Kind = Literal["work", "shelf"]


ADMIN_SCOPE: Final = 255  # the code of an adult's scope in a token (a ceiling is its number)
_NO_SCOPE: Final = 254  # a scope nobody defined


class TooManyRemovalsError(Exception):
    """More removals than one answer may carry: the client reloads the list."""


@dataclass(frozen=True)
class _Ceiling:
    ceiling: int
    kind: str = "ceiling"
    unrestricted: bool = False
    profile_id: str | None = None


def work_need(connection: sa.Connection, work_id: str) -> int:
    """The lowest ceiling (0..99, 99 being X) from which the filter shows this Work; `NEED_NONE`
    when no ceiling does (or the Work does not exist). The filter is monotone in the ceiling."""

    def shown(ceiling: int) -> bool:
        return (
            connection.execute(
                sa.select(sa.literal(1)).where(
                    sa.exists().where(
                        catalog.work.c.id == work_id, age_visible_works_clause(_Ceiling(ceiling))
                    )
                )
            ).first()
            is not None
        )

    if not shown(_MAX_CEILING):
        return NEED_NONE
    low, high = 0, _MAX_CEILING
    while low < high:
        middle = (low + high) // 2
        if shown(middle):
            high = middle
        else:
            low = middle + 1
    return low


def _created_seq(connection: sa.Connection, work_id: str) -> int:
    found = connection.execute(
        sa.select(catalog.work.c.created_seq).where(catalog.work.c.id == work_id)
    ).scalar()
    return int(found or 0)


def _insert(
    connection: sa.Connection, event_type: str, payload: dict[str, Any], now: datetime | None
) -> None:
    connection.execute(
        sa.insert(platform.event).values(
            type=event_type,
            schema_version=EVENT_SCHEMA_VERSION,
            payload=payload,
            occurred_at=now or datetime.now(UTC),
            actor_id=None,
            visibility="ops",
        )
    )


@contextmanager
def track_work_changes(
    connection: sa.Connection, work_ids: Iterable[str], *, now: datetime | None = None
) -> Iterator[None]:
    """Wrap a write that may change what a reader sees of these Works. The need is read before
    and after, in the same transaction; a Work whose need moved gets one event. An error in the
    wrapped write propagates untouched and writes nothing."""
    ids = list(dict.fromkeys(work_ids))
    before = {work_id: work_need(connection, work_id) for work_id in ids}
    yield
    for work_id in ids:
        after = work_need(connection, work_id)
        if after != before[work_id]:
            _insert(
                connection,
                VISIBILITY_EVENT,
                {
                    "work_id": work_id,
                    "need_before": before[work_id],
                    "need_after": after,
                    "created_seq": _created_seq(connection, work_id),
                },
                now,
            )


def _child_scopes(connection: sa.Connection) -> list[_Ceiling]:
    """The children, each as the scope the filter reads (her ceiling and her own profile)."""
    user = identity.user.c
    rows = connection.execute(
        sa.select(user.id, user.max_age_rating).where(user.role == "child")
    ).all()
    return [
        _Ceiling(int(r.max_age_rating), profile_id=str(r.id))
        for r in rows
        if isinstance(r.max_age_rating, int) and 0 <= r.max_age_rating <= _MAX_CEILING
    ]


def _works_on(connection: sa.Connection, shelf_ids: list[str], edition_ids: list[str]) -> set[str]:
    """The Works that a shelf holds an Edition of, or that own one of these Editions."""
    edition = catalog.edition.c
    found: set[str] = set()
    for column, ids in ((edition.shelf_id, shelf_ids), (edition.id, edition_ids)):
        if ids:
            found.update(
                r[0] for r in connection.execute(sa.select(edition.work_id).where(column.in_(ids)))
            )
    return found


@contextmanager
def track_shelf_changes(
    connection: sa.Connection,
    *,
    shelf_ids: Iterable[str] = (),
    edition_ids: Iterable[str] = (),
    now: datetime | None = None,
) -> Iterator[None]:
    """Wrap a write that changes the shelf of an Edition, or the profiles a shelf is open to
    (D-P3). For each child, the Works involved are read through the filter before and after; one
    that moved gets an `ops.profile_visibility` event for her, so `?since=` tells her to drop what
    the shelf list took away (and not to drop what it gave back). An error propagates untouched
    and writes nothing."""
    shelves, editions = list(dict.fromkeys(shelf_ids)), list(dict.fromkeys(edition_ids))
    children = _child_scopes(connection)
    if not children:  # nobody whose view a shelf changes
        yield
        return
    candidates = _works_on(connection, shelves, editions)
    before = {c.profile_id: visible_work_ids(connection, c, candidates) for c in children}
    yield
    candidates |= _works_on(connection, shelves, editions)  # an Edition that moved is the same one
    for child in children:
        after = visible_work_ids(connection, child, candidates)
        was = before[child.profile_id]
        for work_id in sorted(candidates):
            if (work_id in was) != (work_id in after):
                _insert(
                    connection,
                    PROFILE_EVENT,
                    {
                        "work_id": work_id,
                        "profile_id": child.profile_id,
                        "visible_before": work_id in was,
                        "visible_after": work_id in after,
                        "created_seq": _created_seq(connection, work_id),
                    },
                    now,
                )


def record_work_removed(
    connection: sa.Connection, work_id: str, *, now: datetime | None = None
) -> None:
    """Call it **before** the row of the Work goes: the need it had is read from the row."""
    _insert(
        connection,
        WORK_REMOVED_EVENT,
        {
            "work_id": work_id,
            "need_before": work_need(connection, work_id),
            "created_seq": _created_seq(connection, work_id),
        },
        now,
    )


def record_removed(
    connection: sa.Connection, kind: Kind, object_id: str, *, now: datetime | None = None
) -> None:
    if kind == "work":
        record_work_removed(connection, object_id, now=now)
    elif kind == "shelf":
        _insert(connection, SHELF_REMOVED_EVENT, {"shelf_id": object_id}, now)
    else:  # pragma: no cover - the type says so
        raise ValueError(kind)


@dataclass(frozen=True)
class Window:
    floor: int  # the oldest token still answerable (the highest event id the retention purged)
    head: int  # the sequence of the last event ever written (the next token)


def window(connection: sa.Connection) -> Window:
    """The range of tokens that can be answered. The head is the highest value the sequence ever
    took (`AUTOINCREMENT`), so it survives the purge of every row; the floor is what the retention
    recorded when it purged (`instance.events_purged_through`)."""
    head = int(
        connection.execute(
            sa.text("SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'event'), 0)")
        ).scalar_one()
    )
    purged = connection.execute(
        sa.select(sa.func.max(platform.instance.c.events_purged_through))
    ).scalar()
    return Window(floor=int(purged or 0), head=head)


def removed_ids(
    connection: sa.Connection, scope: Any, since_seq: int, *, kind: Kind = "work"
) -> list[str]:
    """The ids that left this reader's list after `since_seq`, in the order they left.

    The caller has checked `since_seq` against `window` (and reads both in one transaction).
    Works: only those the reader could see at the token, by the first event of each. Shelves: the
    removed ones (the route is the admin's)."""
    if kind == "shelf":
        rows = connection.execute(
            sa.select(platform.event.c.payload)
            .where(platform.event.c.type == SHELF_REMOVED_EVENT, platform.event.c.id > since_seq)
            .order_by(platform.event.c.id)
        )
        found = [
            str(r.payload["shelf_id"]) for r in rows if isinstance(r.payload.get("shelf_id"), str)
        ]
        return _capped(found)
    kind_scope = getattr(scope, "kind", "none")
    ceiling = getattr(scope, "ceiling", None)
    if kind_scope == "ceiling" and isinstance(ceiling, int):
        limit: int | None = ceiling
    elif getattr(scope, "unrestricted", False):
        limit = None
    else:
        return []  # a scope nobody defined sees nothing, so nothing left it
    rows = connection.execute(
        sa.select(platform.event.c.id, platform.event.c.type, platform.event.c.payload)
        .where(
            platform.event.c.type.in_((VISIBILITY_EVENT, WORK_REMOVED_EVENT, PROFILE_EVENT)),
            platform.event.c.id > since_seq,
        )
        .order_by(platform.event.c.id)
    )
    first: dict[str, int] = {}  # the need at the token: `need_before` of the first event
    existed: dict[str, bool] = {}  # the Work was there at the token (its creation is not after)
    last: dict[str, int | None] = {}  # the need now; None once removed
    profile_first: dict[str, bool] = {}  # her own: visible at the token (first event's `before`)
    profile_existed: dict[str, bool] = {}
    profile_last: dict[str, bool] = {}  # visible now, by her last event
    own = getattr(scope, "profile_id", None)
    for row in rows:
        payload = row.payload
        if row.type == PROFILE_EVENT:
            member, was, now = (
                payload.get("work_id"),
                payload.get("visible_before"),
                payload.get("visible_after"),
            )
            if payload.get("profile_id") != own or own is None:
                continue  # the events of another profile say nothing of hers
            if (
                not isinstance(member, str)
                or not isinstance(was, bool)
                or not isinstance(now, bool)
            ):
                continue
            if member not in profile_first:
                profile_first[member] = was
                created = payload.get("created_seq")
                profile_existed[member] = isinstance(created, int) and created <= since_seq
            profile_last[member] = now
            continue
        work_id, before = payload.get("work_id"), payload.get("need_before")
        if not isinstance(work_id, str) or not isinstance(before, int):
            continue  # an event this code did not write: never guessed at
        if work_id not in first:
            first[work_id] = before
            created = payload.get("created_seq")
            existed[work_id] = isinstance(created, int) and created <= since_seq
        after = payload.get("need_after")
        last[work_id] = (
            None
            if row.type == WORK_REMOVED_EVENT
            else (after if isinstance(after, int) else NEED_NONE)
        )
    out: list[str] = []
    for work_id, need_at_token in first.items():
        if not existed[work_id]:
            continue  # created after the token: she could not have it, so nothing of it left
        gone, need_now = last[work_id] is None, last[work_id]
        if limit is None:  # an adult sees everything: only a deletion is a removal
            if gone:
                out.append(work_id)
        elif need_at_token <= limit and (gone or (need_now is not None and need_now > limit)):
            out.append(work_id)
    for work_id, was_visible in profile_first.items():  # taken away by a shelf, not by an age
        if (
            profile_existed[work_id]
            and was_visible
            and not profile_last[work_id]
            and work_id not in out
        ):
            out.append(work_id)
    return _capped(out)


def _capped(found: list[str]) -> list[str]:
    if len(found) > MAX_REMOVED:
        raise TooManyRemovalsError
    return found


def scope_code(scope: Any) -> int:
    """What a token remembers of the reader's scope: 255 for an adult, her ceiling for a child,
    254 for a scope nobody defined. A change of any of them since the token is a change of what
    she sees that no event of a Work tells."""
    if getattr(scope, "unrestricted", False):
        return ADMIN_SCOPE
    ceiling = getattr(scope, "ceiling", None)
    if getattr(scope, "kind", "none") == "ceiling" and isinstance(ceiling, int):
        return min(max(ceiling, 0), _MAX_CEILING)
    return _NO_SCOPE


def restore_epoch(connection: sa.Connection) -> int:
    return int(
        connection.execute(sa.select(sa.func.max(platform.instance.c.restore_epoch))).scalar() or 0
    )


def history_marker(connection: sa.Connection, seq: int) -> bytes:
    """Six bytes that identify the event history up to `seq`: a digest of the last event at or
    before it (id, type, time, payload). A backup restored in place starts a history that passes
    the same positions with other events, and a token of the old one does not match it."""
    row = connection.execute(
        sa.select(
            platform.event.c.id,
            platform.event.c.type,
            platform.event.c.occurred_at,
            platform.event.c.payload,
        )
        .where(platform.event.c.id <= seq)
        .order_by(platform.event.c.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return b"\x00" * 6
    body = json.dumps(
        [row.id, row.type, str(row.occurred_at), row.payload], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(body.encode("utf-8")).digest()[:6]
