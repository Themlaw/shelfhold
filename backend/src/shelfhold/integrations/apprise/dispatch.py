"""The sender: an idempotent job over `notification_pending` (EF-INT-006, GAP-6-23).

The event and its pending rows were committed together; this runs afterwards, from the job
`notify.dispatch` (queued after the commit, and swept every few minutes so that a crash or a lost
queue entry costs minutes, not the alert). A row is claimed by moving its `next_try_at` a lease
ahead, sent, then deleted. A crash after the send and before the delete sends the alert again once
the lease ends: delivery is at least once, never zero. A failure keeps the row with a longer wait;
after `MAX_ATTEMPTS` the row is dropped and `notify.failed` is recorded (a banner, never sent by
Apprise). Aggregated events (`job.dead_lettered`, `series.stale`) that wait together for the same
channel become one message.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.events.bus import EventBus
from shelfhold.events.operating import AGGREGATED
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fetcher import LanTargets, Resolver, SystemResolver
from shelfhold.infrastructure.schema import platform
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.integrations.apprise.channels import ChannelStore
from shelfhold.integrations.apprise.destination import validate_channel_url
from shelfhold.integrations.apprise.render import Message, render_notification
from shelfhold.integrations.apprise.send import SendResult, send_with_apprise

log = structlog.get_logger(__name__)

MAX_ATTEMPTS: Final = 5
LEASE: Final = timedelta(minutes=5)  # renewed for each message just before it is sent
LEASE_PER_ROW: Final = timedelta(seconds=30)  # added at claim time: the rows wait for their turn
BATCH: Final = 200
_BACKOFF: Final = timedelta(minutes=5)
_BACKOFF_MAX: Final = timedelta(hours=6)

pending = platform.notification_pending
event = platform.event

Send = Callable[[str, Message], SendResult]
# identifier-keyed upstream text for the message (a series name, say): resolved here, at send time
TextResolver = Callable[[str, Mapping[str, Any]], Mapping[str, str]]


@dataclass(frozen=True)
class DispatchReport:
    sent: int = 0
    failed: int = 0
    dropped: int = 0


@dataclass(frozen=True)
class _Row:
    id: str
    channel: str
    attempts: int
    type: str
    payload: Mapping[str, Any]


def _wait(attempts: int) -> timedelta:
    wait: timedelta = _BACKOFF * (2 ** (attempts - 1))
    return min(wait, _BACKOFF_MAX)


class NotificationDispatcher:
    def __init__(
        self,
        engine: Engine,
        vault: SecretVault,
        clock: Clock,
        *,
        bus: EventBus,
        send: Send = send_with_apprise,
        texts: TextResolver | None = None,
        lan_targets: Callable[[], LanTargets] = LanTargets.empty,
        resolver: Resolver | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._bus = bus
        self._send = send
        self._texts = texts
        self._store = ChannelStore(vault, clock)
        # read at every send: a target removed from `lan_targets` blocks the next ones
        self._lan_targets = lan_targets
        self._resolver = resolver or SystemResolver()

    def run_once(self) -> DispatchReport:
        rows = self._claim()
        urls: dict[str, str] | None
        try:
            with self._engine.connect() as conn:
                urls = self._store.urls(conn) if rows else {}
        except Exception:  # undecryptable: every row counts an attempt, the sweep tries again
            log.warning("notify.channels_unreadable")
            urls = None
        groups: dict[tuple[str, str] | str, list[_Row]] = defaultdict(list)
        for row in rows:
            aggregated = row.type in AGGREGATED
            groups[(row.channel, row.type) if aggregated else row.id].append(row)
        sent = failed = dropped = 0
        for group in groups.values():
            if urls is not None and group[0].channel not in urls:
                self._delete(group)  # the channel was removed since: nothing to send to
                dropped += len(group)
                continue
            self._renew(group)
            if urls is not None and self._deliver(urls[group[0].channel], group):
                self._delete(group)
                sent += len(group)
            else:  # a row that cannot be rendered or checked costs itself an attempt, no more
                failed += len(group)
                dropped += self._retry_or_drop(group)
        return DispatchReport(sent, failed, dropped)

    def _claim(self) -> list[_Row]:
        now = self._clock.now()
        with self._engine.begin() as conn:
            due = conn.execute(
                sa.select(
                    pending.c.id,
                    pending.c.channel,
                    pending.c.attempts,
                    event.c.type,
                    event.c.payload,
                )
                .select_from(pending.join(event, event.c.id == pending.c.event_id))
                .where(pending.c.next_try_at <= now)
                .order_by(pending.c.next_try_at, pending.c.id)
                .limit(BATCH)
            ).all()
            lease = LEASE + LEASE_PER_ROW * len(due)
            claimed: list[_Row] = []
            for r in due:  # compare-and-swap: a concurrent dispatcher loses the row
                won = conn.execute(
                    sa.update(pending)
                    .where(pending.c.id == r.id, pending.c.next_try_at <= now)
                    .values(next_try_at=now + lease)
                ).rowcount
                if won:
                    claimed.append(_Row(r.id, r.channel, r.attempts, r.type, r.payload or {}))
        return claimed

    def _renew(self, group: list[_Row]) -> None:
        """A long batch must not let another worker take a row we are about to send."""
        until = self._clock.now() + LEASE
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(pending)
                .where(pending.c.id.in_([r.id for r in group]))
                .values(next_try_at=until)
            )

    def _deliver(self, url: str, group: list[_Row]) -> bool:
        first = group[0]
        try:
            upstream = dict(self._texts(first.type, first.payload)) if self._texts else None
            message = render_notification(
                first.type,
                first.payload if len(group) == 1 else {},
                upstream=upstream,
                count=len(group),
            )
            # the SSRF verdict is taken again now, with the lan_targets of now (ENF-SEC-016)
            asyncio.run(validate_channel_url(url, lan=self._lan_targets(), resolver=self._resolver))
            return self._send(url, message).ok
        except Exception:
            log.warning("notify.send_failed", event_type=first.type)  # never the URL or the text
            return False

    def _delete(self, group: list[_Row]) -> None:
        with self._engine.begin() as conn:
            conn.execute(sa.delete(pending).where(pending.c.id.in_([r.id for r in group])))

    def _retry_or_drop(self, group: list[_Row]) -> int:
        now = self._clock.now()
        dropped = 0
        with self._engine.begin() as conn:
            for row in group:
                attempts = row.attempts + 1
                if attempts >= MAX_ATTEMPTS:
                    conn.execute(sa.delete(pending).where(pending.c.id == row.id))
                    dropped += 1
                else:
                    conn.execute(
                        sa.update(pending)
                        .where(pending.c.id == row.id)
                        .values(attempts=attempts, next_try_at=now + _wait(attempts))
                    )
            if dropped:
                self._bus.publish(
                    conn, "notify.failed", {"reason": "retries_exhausted", "count": dropped}
                )
        return dropped
