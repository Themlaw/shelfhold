"""The feed of one reader's SSE stream (EF-INT-002): what to send, when, and when to say "reload".

Transport-free and synchronous: the route owns the socket and the waiting, `Feed.poll()` answers
"what is due now" from the database and an injected clock, so a test moves time by hand.

What a hidden event must never betray, and how:

- **The filter runs at send time, with the reader's profile**: every row goes through the
  `project` function the route hands over (`project_event`, the one filter of L09); a row it
  refuses produces no byte. The emitter's profile is never consulted.
- **No gap in the identifiers**: the `id:` of a frame is a sealed cursor, not the row number:
  random nonce, so two cursors say nothing of the distance between them. A row the reader may
  not see advances the internal cursor and nothing else.
- **No tell in the heartbeat**: it is sent on a fixed schedule of the clock (`HEARTBEAT_S`), never
  moved by an event, hidden or not, and it always carries a cursor (the SSE `id` field with an
  empty `data` moves the browser's last id without dispatching anything).
- **`resync_required` is never caused by a hidden event**: it answers a cursor that is not
  trustworthy (forged, another profile's, from before a restore, from before a purge, a profile
  whose role or ceiling changed since) and a restore while the stream is open. The answer is a
  normal 200 stream holding that one frame, the same for every reason.

The cursor is the `?since=` token of M3 L11 (position, `restore_epoch`, history marker, scope
code, sealed under the instance key), issued for the profile it belongs to. The route seals and
opens it; this module only decides what a cursor means. The floor is the one of L11 (`window`):
the retention and the purge of a Work (`invalidate_tokens`) raise it, and a cursor under it is
`resync_required`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import history_marker, restore_epoch, scope_code, window
from shelfhold.events.upcast import UnreadableVersionError, upcast_payload
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import platform

__all__ = ["BATCH", "HEARTBEAT_S", "RESYNC_FRAME", "Cursor", "Feed"]

log = structlog.get_logger(__name__)

HEARTBEAT_S: Final = 15.0
BATCH: Final = 200  # rows evaluated per `poll`; a longer backlog takes several polls
RESYNC_FRAME: Final = "id:\nevent: resync_required\ndata: {}\n\n"  # `id:` empties the browser's id


@dataclass(frozen=True)
class Cursor:
    """What a sealed id remembers: where the reader was, in which history, with which rights."""

    seq: int
    epoch: int
    marker: bytes
    scope: int


# What the route hands over: the reader's projection of a row, as the JSON of its frame (None: not
# for this reader). The title and what hides it stay in `events.activity`: this module never
# touches them.
Project = Callable[[Connection, Any, dict[str, Any]], dict[str, Any] | None]
Seal = Callable[[Cursor], str]
Unseal = Callable[[str], Cursor | None]  # None: not a cursor of this reader, whatever the reason


class Feed:
    """One reader's stream. `start()` once (a replay or a fresh start, or a `resync_required`),
    then `poll()` each time something may be due, waiting `seconds_to_heartbeat()` at most."""

    def __init__(
        self,
        engine: Engine,
        *,
        seal: Seal,
        unseal: Unseal,
        scope: Any,
        project: Project,
        clock: Clock,
        alive: Callable[[], bool],
        scope_now: Callable[[], Any],
        last_event_id: str | None = None,
        heartbeat_s: float = HEARTBEAT_S,
        upcast: Callable[[str, int, dict[str, Any]], dict[str, Any]] = upcast_payload,
    ) -> None:
        self._engine = engine
        self._seal = seal
        self._unseal = unseal
        self._scope_code = scope_code(scope)
        self._project = project
        self._clock = clock
        self._alive = alive
        self._scope_now = scope_now
        self._last_event_id = last_event_id
        self._heartbeat_s = heartbeat_s
        self._upcast = upcast
        self._seq = 0
        self._epoch = 0
        self._floor = 0
        self._next_beat = 0.0
        self.closed = False
        self.backlog = False  # the last poll filled its batch: poll again without waiting

    # -- the clock -------------------------------------------------------------------------

    def _now(self) -> float:
        return self._clock.now().timestamp()

    def seconds_to_heartbeat(self) -> float:
        return max(0.0, self._next_beat - self._now())

    # -- start -----------------------------------------------------------------------------

    def _resync(self) -> list[str]:
        self.closed = True
        return [RESYNC_FRAME]

    def start(self) -> list[str]:
        """No `Last-Event-ID`: the stream starts at the head of the sequence. One that is not a
        cursor this stream can trust: `resync_required`, whatever the reason."""
        with self._engine.connect() as connection:
            self._epoch = restore_epoch(connection)
            seen = window(connection)
            self._floor = seen.floor
            self._next_beat = self._now() + self._heartbeat_s
            if self._last_event_id is None:
                self._seq = seen.head
                return []
            cursor = self._unseal(self._last_event_id)
            if (
                cursor is None
                or cursor.scope != self._scope_code  # her role or her ceiling changed
                or cursor.epoch != self._epoch  # a restore since
                or cursor.seq > seen.head  # not issued yet: another history
                or cursor.seq < seen.floor  # the retention or a purge took what follows
                or not self._same_history(connection, cursor)  # same places, other rows
            ):
                return self._resync()
        self._seq = cursor.seq
        return []

    @staticmethod
    def _same_history(connection: Connection, cursor: Cursor) -> bool:
        """The marker is the digest of the row at the cursor. A row that is gone (a purge or the
        retention erased it) cannot be compared, and its going is the floor's business (every
        purge that erases an event raises it): the cursor is not refused for it, so that the
        erasure of a row a reader never saw tells her nothing."""
        row = connection.execute(
            sa.select(platform.event.c.id).where(platform.event.c.id == cursor.seq)
        ).first()
        return row is None or cursor.marker == history_marker(connection, cursor.seq)

    # -- poll ------------------------------------------------------------------------------

    def _moved(self, connection: Connection) -> bool:
        """What turns every frame of this stream stale: a restore, a purge or the retention (the
        floor moved: the same for every reader), her role or her ceiling."""
        return (
            restore_epoch(connection) != self._epoch
            or window(connection).floor != self._floor
            or scope_code(self._scope_now()) != self._scope_code
        )

    def _token(self, connection: Connection) -> str:
        return self._seal(
            Cursor(self._seq, self._epoch, history_marker(connection, self._seq), self._scope_code)
        )

    def _frame(self, connection: Connection, row: Any, shown: dict[str, Any]) -> str:
        data = json.dumps(shown, separators=(",", ":"))  # the row number is not in it: gaps
        return f"id: {self._token(connection)}\nevent: {row.type}\ndata: {data}\n\n"

    def poll(self) -> list[str]:
        """The frames due now, in order. The credential is checked first, and again before the
        batch is sent: a revoked session gets nothing more."""
        if self.closed:
            return []
        if not self._alive():
            self.closed = True  # silent: the browser reconnects and is refused at the door
            return []
        frames: list[str] = []
        with self._engine.connect() as connection:
            if self._moved(connection):
                return self._resync()  # restored, purged, or her role or ceiling changed
            rows = connection.execute(
                sa.select(platform.event)
                .where(platform.event.c.id > self._seq)
                .order_by(platform.event.c.id)
                .limit(BATCH)
            ).all()
            self.backlog = len(rows) == BATCH
            for row in rows:
                self._seq = int(row.id)
                try:
                    payload = self._upcast(row.type, row.schema_version, dict(row.payload or {}))
                except UnreadableVersionError:
                    log.warning("event.stream_unreadable_version", event_id=int(row.id))
                    continue  # no byte for a row nobody can present, for any reader
                shown = self._project(connection, row, payload)
                if shown is not None:
                    frames.append(self._frame(connection, row, shown))
            if not self._alive():  # revoked while the batch was read: nothing of it leaves
                self.closed = True
                return []
            if self._moved(
                connection
            ):  # her ceiling moved while it was filtered: none of it leaves
                return self._resync()
            if self._now() >= self._next_beat:
                frames.append(f"id: {self._token(connection)}\n: keepalive\n\n")
                self._next_beat += self._heartbeat_s
                if self._next_beat <= self._now():  # a long stall is one beat, not a burst
                    self._next_beat = self._now() + self._heartbeat_s
        return frames
