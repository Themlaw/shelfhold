"""The operating events of the Kavita destination, on the bus (EF-INT-006).

The client announces `target.*` events with free-form details; the bus only keeps ULIDs, codes,
numbers and booleans. Each detail is offered on its own and the ones the bus refuses are dropped
(they are in the log line), so the event itself is never lost to a title or a path.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import Engine

from shelfhold.events.bus import EventBus, EventPayloadError, UnknownEventType, check_payload
from shelfhold.infrastructure.db import write_transaction

__all__ = ["BusTargetEvents"]

log = structlog.get_logger("shelfhold.export")


class BusTargetEvents:
    def __init__(self, engine: Engine, bus: EventBus) -> None:
        self._engine = engine
        self._bus = bus

    def emit(self, code: str, **detail: Any) -> None:
        kept: dict[str, object] = {}
        for key, value in detail.items():
            try:
                check_payload({key: value}, code)
            except EventPayloadError:
                continue
            kept[key] = value
        log.warning(code, **{k: v for k, v in detail.items() if isinstance(v, str | int | bool)})
        try:
            with write_transaction(self._engine) as connection:
                published = self._bus.publish(connection, code, kept)
        except (UnknownEventType, EventPayloadError):
            return
        self._bus.deliver([published])
