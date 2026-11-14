"""The router and the sender of per-person notifications (EF-INT-021).

`NotificationRouter` listens to the bus. For an event that is about a person (a request that
arrived, a chapter of a series they follow) it decides who is told, through the one projection of
the application (`project_event`: the role, the visibility, the owner and, for a child, the reader
scope of the catalogue), and queues one item per person in `notification_digest`. It stores
identifiers and counts, never a title: a title is read when the message is built.

`NotificationSender` is the job. Each run it notices outages that have lasted an hour, makes sure
an admin with a `withdrawal_pending` waiting has tomorrow's reminder (it only reads: the exit is
`shelf withdraw`, a reserved action nobody here can take), then sends what is due. Before a
message is built **every item is projected again** for the person as they are now: a Work rerated
after the event, a ceiling lowered, a role changed, a profile removed, and the item is dropped. A
failed send keeps the row with a longer wait; after `MAX_ATTEMPTS` it is dropped (never retried
for ever, never logged with its text).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.events.activity import ActivityEvent, Viewer
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.events.projection import project_event
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog, identity, platform
from shelfhold.integrations.apprise.render import Message, neutralize_markdown
from shelfhold.integrations.notify.routing import (
    OUTAGE_AFTER,
    OUTAGE_EVENTS,
    OUTAGE_GAP,
    Recipient,
    classify,
    delivery_time,
)

log = structlog.get_logger(__name__)

MAX_ATTEMPTS: Final = 5
LEASE: Final = timedelta(minutes=5)
_BACKOFF: Final = timedelta(minutes=5)
_BACKOFF_MAX: Final = timedelta(hours=6)
REMINDER_CHECK: Final = timedelta(hours=1)
_OUTAGE_KEY: Final = "notify.outage."
_REMINDER_KEY: Final = "notify.reminder.checked_at"
_PAGE: Final = 500

digest = identity.notification_digest
Deliver = Callable[[str, Message], bool]
PendingWithdrawals = Callable[[Connection], int]
Item = dict[str, Any]


@dataclass(frozen=True)
class SendReport:
    sent: int = 0
    failed: int = 0
    dropped: int = 0


def _recipient(row: Any) -> Recipient:
    return Recipient(
        user_id=row.id, username=row.username, role=row.role, timezone=row.timezone,
        notify_mode=row.notify_mode, quiet_start=row.quiet_start, quiet_end=row.quiet_end,
    )  # fmt: skip


_PERSON = (
    identity.user.c.id, identity.user.c.username, identity.user.c.role, identity.user.c.timezone,
    identity.user.c.notify_mode, identity.user.c.quiet_start, identity.user.c.quiet_end,
)  # fmt: skip


def _person(connection: Connection, user_id: str) -> Recipient | None:
    row = connection.execute(sa.select(*_PERSON).where(identity.user.c.id == user_id)).first()
    return None if row is None else _recipient(row)


def _series_of(connection: Connection, work_id: str) -> list[str]:
    member = catalog.series_member.c
    return list(
        connection.execute(
            sa.select(member.series_id).where(member.work_id == work_id).order_by(member.series_id)
        ).scalars()
    )


def _merge(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Two payloads for the same person and the same instant become one: chapters of the same
    Work add up, any other item is kept once."""
    items: list[Item] = [dict(i) for i in old.get("items", [])]
    for item in new.get("items", []):
        for held in items:
            same = held["kind"] == item["kind"] and all(
                held.get(k) == item.get(k)
                for k in ("work_id", "vis", "owner_id", "request_id", "type", "start_id")
            )
            if same:
                if item["kind"] == "chapters":
                    held["count"] = int(held.get("count", 0)) + int(item.get("count", 0))
                break
        else:
            items.append(dict(item))
    return {
        "urgent": bool(old.get("urgent")) or bool(new.get("urgent")),
        "attempts": max(int(old.get("attempts", 0)), int(new.get("attempts", 0))),
        "items": items,
    }


def _put(
    connection: Connection, user_id: str, due_at: datetime, payload: Mapping[str, Any]
) -> None:
    held = connection.execute(
        sa.select(digest.c.payload).where(digest.c.user_id == user_id, digest.c.due_at == due_at)
    ).scalar_one_or_none()
    if held is None:
        connection.execute(
            sa.insert(digest).values(user_id=user_id, due_at=due_at, payload=_merge({}, payload))
        )
    else:
        connection.execute(
            sa.update(digest)
            .where(digest.c.user_id == user_id, digest.c.due_at == due_at)
            .values(payload=_merge(held, payload))
        )


class NotificationRouter:
    def __init__(
        self, engine: Engine, clock: Clock, *, wake: Callable[[], None] | None = None
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._wake = wake

    def subscribe(self, bus: EventBus) -> None:
        for event_type in ("ops.request_arrived", "unit.stored"):
            bus.subscribe(self.on_event, event_type=event_type)

    def on_event(self, event: PublishedEvent) -> None:
        kind = classify(event.type)
        urgent_queued = False
        with write_transaction(self._engine) as connection:
            actor = connection.execute(
                sa.select(platform.event.c.actor_id).where(platform.event.c.id == event.id)
            ).scalar_one_or_none()
            if kind == "request_arrived":
                urgent_queued = self._request_arrived(connection, event)
            elif kind == "chapters":
                self._chapters(connection, event, actor)
        if urgent_queued and self._wake is not None:
            try:
                self._wake()
            except Exception:  # a sweep that cannot be woken still runs on its own schedule
                log.warning("notify.wake_failed")

    # -- the two kinds of event -----------------------------------------------------------------

    def _request_arrived(self, connection: Connection, event: PublishedEvent) -> bool:
        request_id = event.payload.get("request_id")
        row = connection.execute(
            sa.select(
                identity.request.c.requester_user_id,
                identity.request.c.work_id,
                identity.request.c.visibility,
            ).where(identity.request.c.id == request_id)
        ).first()
        if row is None:
            return False
        person = _person(connection, row.requester_user_id)
        if person is None:
            return False
        item: Item = {
            "kind": "request_arrived", "request_id": request_id, "work_id": row.work_id,
            "vis": row.visibility, "owner_id": person.user_id, "owner_role": person.role,
        }  # fmt: skip
        if not _visible(connection, person, item):
            return False
        self._queue(connection, person, item, urgent=True)
        return True

    def _chapters(self, connection: Connection, event: PublishedEvent, actor: str | None) -> None:
        work_id = event.payload.get("work_id")
        if not isinstance(work_id, str):
            return
        state = identity.user_state
        followers = connection.execute(
            sa.select(*_PERSON)
            .select_from(state.join(identity.user, identity.user.c.id == state.c.user_id))
            .where(state.c.work_id == work_id, state.c.followed.is_(True))
        ).all()
        series = _series_of(connection, work_id)
        owner_role = None
        if event.visibility != "ops" and actor is not None:
            person = _person(connection, actor)
            owner_role = person.role if person else None
        for row in followers:
            person = _recipient(row)
            vis: str
            owner_id: str | None
            role: str | None
            if event.visibility == "ops":  # the library's own news is for whoever follows
                vis, owner_id, role = "household", person.user_id, person.role
            else:  # a request's own acquisition keeps the view of the request
                vis, owner_id, role = event.visibility, actor, owner_role
            item: Item = {
                "kind": "chapters", "work_id": work_id, "series_id": series[0] if series else None,
                "series_ids": series, "vis": vis, "owner_id": owner_id, "owner_role": role,
                "count": 1,
            }  # fmt: skip
            if _visible(connection, person, item):
                self._queue(connection, person, item, urgent=False)

    def _queue(
        self, connection: Connection, person: Recipient, item: Item, *, urgent: bool
    ) -> None:
        immediate = urgent or person.notify_mode == "immediate"
        due = delivery_time(person, urgent=immediate, now=self._clock.now())
        _put(connection, person.user_id, due, {"urgent": immediate, "items": [item]})


def _activity(item: Item) -> ActivityEvent:
    refs: dict[str, object] = {}
    if item.get("work_id"):
        refs["work_id"] = item["work_id"]
    if item.get("series_ids"):
        refs["series_ids"] = list(item["series_ids"])
    event_type = {"chapters": "unit.stored", "request_arrived": "ops.request_arrived"}.get(
        item["kind"], str(item.get("type", "outage"))
    )
    return ActivityEvent(
        0, event_type, str(item["vis"]), refs, owner_id=item.get("owner_id"),
        owner_role=item.get("owner_role"),
    )  # fmt: skip


def _visible(connection: Connection, person: Recipient, item: Item) -> bool:
    """The projection of the item for this person now: None is not theirs to be told."""
    seen = _project(connection, person, item)
    return seen is not None


def _project(connection: Connection, person: Recipient, item: Item) -> Any:
    return project_event(
        connection, Viewer(person.user_id, person.role),
        reader_scope(connection, person.user_id), _activity(item),
    )  # fmt: skip


def _masked(connection: Connection, person: Recipient, item: Item) -> bool | None:
    seen = _project(connection, person, item)
    return None if seen is None else bool(seen.title_masked)


class NotificationSender:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        *,
        deliver: Deliver,
        pending_withdrawals: PendingWithdrawals,
        app_url: str | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._deliver = deliver
        self._withdrawals = pending_withdrawals
        self._app_url = app_url

    def run_once(self) -> SendReport:
        now = self._clock.now()
        with write_transaction(self._engine) as connection:
            for event_type in sorted(OUTAGE_EVENTS):
                self._scan_outage(connection, event_type, now)
            self._ensure_reminders(connection, now)
        return self._send_due(now)

    # -- what the sweep notices -----------------------------------------------------------------

    def _scan_outage(self, connection: Connection, event_type: str, now: datetime) -> None:
        """An outage is a run of the same failure whose events are never more than `OUTAGE_GAP`
        apart. Once its first event is over an hour old and it is still going, the admins are told,
        once (the first event of the run is remembered)."""
        event = platform.event
        start_id: int | None = None
        start_at: datetime | None = None
        cursor: int | None = None
        walking = True
        while walking:  # newest first, by pages, back to the first event of the run
            query = (
                sa.select(event.c.id, event.c.occurred_at)
                .where(event.c.type == event_type)
                .order_by(event.c.id.desc())
                .limit(_PAGE)
            )
            if cursor is not None:
                query = query.where(event.c.id < cursor)
            rows = connection.execute(query).all()
            for row in rows:
                if start_at is None and now - row.occurred_at > OUTAGE_GAP:
                    return  # nothing failed lately: no outage going on
                if start_at is not None and start_at - row.occurred_at > OUTAGE_GAP:
                    walking = False  # a recovery lies between: the run starts after it
                    break
                start_id, start_at = row.id, row.occurred_at
            else:
                walking = len(rows) == _PAGE
                cursor = rows[-1].id if rows else None
        if start_id is None or start_at is None or now - start_at <= OUTAGE_AFTER:
            return
        key = f"{_OUTAGE_KEY}{event_type}"
        told = connection.execute(
            sa.select(platform.app_setting.c.value).where(platform.app_setting.c.key == key)
        ).scalar_one_or_none()
        if told == str(start_id):
            return
        if told is None:
            connection.execute(sa.insert(platform.app_setting).values(key=key, value=str(start_id)))
        else:
            connection.execute(
                sa.update(platform.app_setting)
                .where(platform.app_setting.c.key == key)
                .values(value=str(start_id))
            )
        for admin in self._admins(connection):
            item: Item = {
                "kind": "outage", "type": event_type, "start_id": start_id, "vis": "ops",
                "owner_id": None, "owner_role": None,
            }  # fmt: skip
            if _visible(connection, admin, item):
                _put(
                    connection,
                    admin.user_id,
                    delivery_time(admin, urgent=True, now=now, bypass_quiet=True),
                    {"urgent": True, "items": [item]},
                )

    def _ensure_reminders(self, connection: Connection, now: datetime) -> None:
        """A `withdrawal_pending` that is still open keeps tomorrow's digest of every admin, once a
        day, until it is gone. The count is read here and again when the message is built; nothing
        is applied."""
        key = _REMINDER_KEY
        setting = platform.app_setting
        checked = connection.execute(
            sa.select(setting.c.value).where(setting.c.key == key)
        ).scalar_one_or_none()
        if checked is not None and now - datetime.fromisoformat(checked) < REMINDER_CHECK:
            return
        stamp = now.isoformat()
        if checked is None:
            connection.execute(sa.insert(setting).values(key=key, value=stamp))
        else:
            connection.execute(sa.update(setting).where(setting.c.key == key).values(value=stamp))
        if self._withdrawals(connection) <= 0:
            return
        for admin in self._admins(connection):
            _put(
                connection,
                admin.user_id,
                delivery_time(admin, urgent=False, now=now),
                {"urgent": False, "items": [{"kind": "withdrawal_pending", "vis": "ops"}]},
            )

    @staticmethod
    def _admins(connection: Connection) -> list[Recipient]:
        rows = connection.execute(sa.select(*_PERSON).where(identity.user.c.role == "admin")).all()
        return [_recipient(r) for r in rows]

    # -- what is due ----------------------------------------------------------------------------

    def _send_due(self, now: datetime) -> SendReport:
        with self._engine.connect() as connection:
            due = connection.execute(
                sa.select(digest.c.user_id, digest.c.due_at)
                .where(digest.c.due_at <= now)
                .order_by(digest.c.due_at)
            ).all()
        sent = failed = dropped = 0
        for row in due:
            claimed = self._claim(row.user_id, row.due_at, now)
            if claimed is None:
                continue
            leased_at, payload = claimed
            outcome = self._send_one(row.user_id, leased_at, payload, now)
            sent += outcome == "sent"
            failed += outcome == "failed"
            dropped += outcome == "dropped"
        return SendReport(sent, failed, dropped)

    def _claim(self, user_id: str, due_at: datetime, now: datetime) -> tuple[datetime, Any] | None:
        """Move the row a lease ahead: a concurrent sweep that read it too finds nothing."""
        with write_transaction(self._engine) as connection:
            payload = connection.execute(
                sa.select(digest.c.payload).where(
                    digest.c.user_id == user_id, digest.c.due_at == due_at
                )
            ).scalar_one_or_none()
            if payload is None:
                return None
            connection.execute(
                sa.delete(digest).where(digest.c.user_id == user_id, digest.c.due_at == due_at)
            )
            leased = now + LEASE
            _put(connection, user_id, leased, payload)
            return leased, payload

    def _send_one(self, user_id: str, leased_at: datetime, payload: Any, now: datetime) -> str:
        with self._engine.connect() as connection:
            person = _person(connection, user_id)
            message = None if person is None else self._build(connection, person, payload)
        if message is None:  # a person who is gone, or nothing left that is theirs to be told
            self._finish(user_id, leased_at)
            return "dropped"
        try:
            delivered = bool(self._deliver(user_id, message))
        except Exception:
            delivered = False  # never the text of the error: it may carry a URL
        if delivered:
            self._finish(user_id, leased_at)
            return "sent"
        attempts = int(payload.get("attempts", 0)) + 1
        if attempts >= MAX_ATTEMPTS:
            self._finish(user_id, leased_at)
            log.warning("notify.digest_dropped", attempts=attempts)
            return "dropped"
        wait = min(_BACKOFF * (2 ** (attempts - 1)), _BACKOFF_MAX)
        log.warning("notify.digest_failed", attempts=attempts)
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.delete(digest).where(digest.c.user_id == user_id, digest.c.due_at == leased_at)
            )
            _put(connection, user_id, now + wait, {**payload, "attempts": attempts})
        return "failed"

    def _finish(self, user_id: str, due_at: datetime) -> None:
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.delete(digest).where(digest.c.user_id == user_id, digest.c.due_at == due_at)
            )

    # -- the message ----------------------------------------------------------------------------

    def _build(self, connection: Connection, person: Recipient, payload: Any) -> Message | None:
        """The text of a notice. It carries **no title, no series name and no request title**: the
        only way out is the instance's channels, which several people read (a child included), so
        a title could reach someone it must not. It says how many news there are, for whom (the
        profile's name, never a child's) and where to look. Titles in clear wait for a destination
        per profile (debt, M4)."""
        chapters = arrivals = outages = 0
        reminders = False
        alive = self._alive(connection, payload)
        for item in payload.get("items", []):
            kind = item["kind"]
            if kind == "withdrawal_pending":
                reminders = reminders or (
                    person.role == "admin" and self._withdrawals(connection) > 0
                )
                continue
            if _masked(connection, person, item) is None:
                continue  # not theirs to be told, now
            if kind in ("chapters", "request_arrived") and item.get("work_id") not in alive:
                continue  # purged since the event: nothing left to tell
            if kind == "outage":
                outages += 1
            elif kind == "request_arrived":
                arrivals += 1
            else:
                chapters += int(item.get("count", 1))
        if not (chapters or arrivals or outages or reminders):
            return None
        lines: list[str] = []
        if person.role != "child" and person.username:  # a child is never named in a shared place
            lines.append(f"For {neutralize_markdown(person.username)}:")
        if chapters:
            lines.append(f"- {chapters} new chapter(s)")
        if arrivals:
            lines.append(f"- {arrivals} request(s) arrived")
        if outages:
            lines.append(f"- {outages} outage(s) have lasted over one hour")
        if reminders:
            lines.append("- A withdrawal is still pending: it waits for an admin")
        if self._app_url:
            lines.append(f"Open Shelfhold: {self._app_url}")
        only_arrival = arrivals and not (chapters or outages or reminders)
        title = (
            "Shelfhold - a request arrived" if only_arrival
            else "Shelfhold - outage over one hour" if outages
            else "Shelfhold - daily digest"
        )  # fmt: skip
        return Message(title=title, body="\n".join(lines))

    @staticmethod
    def _alive(connection: Connection, payload: Any) -> set[str]:
        """Of the Works the items name, the ones the catalogue still holds."""
        ids = {i["work_id"] for i in payload.get("items", []) if i.get("work_id")}
        if not ids:
            return set()
        rows = connection.execute(
            sa.select(catalog.work.c.id).where(catalog.work.c.id.in_(sorted(ids)))
        )
        return {r[0] for r in rows}
