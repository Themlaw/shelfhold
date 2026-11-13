"""Kavita's scan events as a reason to look sooner (EF-EXP-034, M3; D11).

Kavita announces `SeriesAdded` and `ScanSeries` on its hub (SignalR, JSON protocol, over a
connection authenticated with the dedicated key). This module is what Shelfhold does with them,
with a rule that makes a forged event harmless: **an event is never a proof**. It only says "look
now" for the destinations of its library that wait for an arrival; what confirms the arrival is
always the API of Kavita, read by the same poller as ever (`ArrivalPoller`), which stays the
fallback and the only road when no event comes. An event carries no state, no path and no series
that is believed; a flood of them costs one look per destination and per interval.

No WebSocket client is bundled (D11: a new dependency needs wheels for amd64 and arm64, and a
way to authenticate by key): without a transport, nothing feeds `ArrivalEventWatcher` and the
polling alone confirms. The transport only has to hand the bytes it reads to `HubSession`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

import structlog

from shelfhold.export.application.target_state import DestinationPausedError
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.polling import ArrivalPoller
from shelfhold.infrastructure.clock import Clock

__all__ = ["ArrivalEventWatcher", "HubSession", "ScanEvent", "parse_hub_frames"]

log = structlog.get_logger(__name__)

RECORD_SEPARATOR = "\x1e"
WATCHED = frozenset({"SeriesAdded", "ScanSeries"})  # a removal or a progress is no arrival
MAX_FRAME = 64 * 1024
MAX_FRAMES = 256  # per chunk read from the hub
MIN_INTERVAL = timedelta(seconds=30)


@dataclass(frozen=True)
class ScanEvent:
    kind: str  # `SeriesAdded` or `ScanSeries`
    library_id: int  # the only field read: which library to look at


def _event_of(frame: str) -> ScanEvent | None:
    """One hub message → the event, or None for anything that is not a well-formed announcement
    of a watched kind. Every field is checked: the bytes come from the network."""
    if len(frame) > MAX_FRAME:
        return None
    try:
        message = json.loads(frame)
    except (ValueError, RecursionError):
        return None
    if not isinstance(message, dict) or message.get("type") != 1:  # 1: an invocation
        return None
    target, arguments = message.get("target"), message.get("arguments")
    if target not in WATCHED or not isinstance(arguments, list) or not arguments:
        return None
    first = arguments[0]
    body = first.get("body") if isinstance(first, dict) else None
    library = body.get("libraryId") if isinstance(body, dict) else None
    if not isinstance(library, int) or isinstance(library, bool):
        return None
    return ScanEvent(target, library)


def _frames(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return [f for f in text.split(RECORD_SEPARATOR) if f][:MAX_FRAMES]


def parse_hub_frames(data: bytes) -> list[ScanEvent]:
    """The watched events of the frames in `data` (record-separated), the rest dropped."""
    return [event for frame in _frames(data) if (event := _event_of(frame)) is not None]


class _State(Enum):
    HANDSHAKE = "handshake"
    OPEN = "open"
    CLOSED = "closed"


class HubSession:
    """The state of one hub connection. Nothing is read as an event before Kavita acknowledged
    the handshake (`{}`) of the connection the transport authenticated; a refusal, or anything
    else where the acknowledgement should be, closes the session for good."""

    def __init__(self) -> None:
        self._state = _State.HANDSHAKE

    def handshake_request(self) -> bytes:
        return (json.dumps({"protocol": "json", "version": 1}) + RECORD_SEPARATOR).encode()

    def receive(self, data: bytes) -> list[ScanEvent]:
        frames = _frames(data)
        if self._state is _State.CLOSED:
            return []
        if self._state is _State.HANDSHAKE:
            if not frames or frames[0] != "{}":  # an error, or a message before the answer
                self._state = _State.CLOSED
                return []
            self._state = _State.OPEN
            frames = frames[1:]
        return [event for frame in frames if (event := _event_of(frame)) is not None]


class ArrivalEventWatcher:
    """Turns events into looks: for each library named, the destinations that wait for an arrival
    are polled now, at most once per `min_interval` each."""

    def __init__(
        self,
        clock: Clock,
        poller: ArrivalPoller,
        *,
        min_interval: timedelta = MIN_INTERVAL,
    ) -> None:
        self._clock = clock
        self._poller = poller
        self._min_interval = min_interval
        self._last: dict[str, datetime] = {}  # destination -> when it was last looked at

    async def on_events(self, client: KavitaClient, events: Sequence[ScanEvent]) -> list[str]:
        """The destinations looked at. Confirmation, delay and `target.not_indexed` are the
        poller's, unchanged. The events are those of the hub of `client`'s target: only that
        target's destinations are looked at (a library id means something per server), and a
        client not bound to a target is not vouched for, so nothing is looked at."""
        target_id = client.target_id
        looked: list[str] = []
        if target_id is None:
            return looked
        for library_id in sorted({event.library_id for event in events}):
            for destination_id in self._poller.waiting_destinations(target_id, library_id):
                now = self._clock.now()
                last = self._last.get(destination_id)
                if last is not None and now - last < self._min_interval:
                    continue
                self._last[destination_id] = now
                try:
                    await self._poller.poll(client, destination_id)
                except (TargetError, DestinationPausedError) as error:
                    log.info(
                        "arrival.event_poll_failed", destination_id=destination_id,
                        reason=type(error).__name__,
                    )  # fmt: skip
                    continue
                looked.append(destination_id)
        return looked
