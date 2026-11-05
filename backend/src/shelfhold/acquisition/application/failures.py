"""What a failure does to the database: the content counter, the service pause, source health.

Only `content` is counted in `source_failure`. A pause lives in `source_config.health` (one row per
plugin), is announced once when it starts, and ends with `resume` (the admin renewed the secret,
updated the tool...). Nothing here ever touches `source_config.consecutive_failures`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.events.waiting_state import WaitingState
from shelfhold.infrastructure.schema import acquisition

_PAUSE_EVENTS = {
    "auth": "source.auth_expired",
    "version": "source.version_unsupported",
    "schema": "source.broken",
}


@dataclass(frozen=True)
class Pause:
    failure_class: str
    event_type: str
    waiting_state: str | None  # an EF-INT-023 state when the pause is one

    @property
    def label(self) -> str:
        """What a report says: the waiting state, else the class."""
        return self.waiting_state or self.failure_class


def pause_of(failure_class: str, error: BaseException) -> Pause:
    code = getattr(error, "code", "")
    event = code if code == "source.auth_mode_unsupported" else _PAUSE_EVENTS[failure_class]
    state = getattr(error, "waiting_state", None)
    if state is None and failure_class == "auth":
        state = WaitingState.AUTH_EXPIRED
    return Pause(failure_class, event, str(state) if state else None)


class FailureLedger:
    def __init__(self, plugin_id: str) -> None:
        self._plugin_id = plugin_id

    # -- content counter ---------------------------------------------------------------------
    @staticmethod
    def count_content(
        connection: Connection, binding_id: str, limit_key: str, code: str, now: datetime
    ) -> None:
        table = acquisition.source_failure
        stmt = sqlite_insert(table).values(
            source_binding_id=binding_id, limit_key=limit_key, failure_class="content",
            consecutive_content_errors=1, last_error_code=code, last_error_at=now,
        )  # fmt: skip
        connection.execute(
            stmt.on_conflict_do_update(
                index_elements=["source_binding_id", "limit_key", "failure_class"],
                set_={
                    "consecutive_content_errors": table.c.consecutive_content_errors + 1,
                    "last_error_code": code,
                    "last_error_at": now,
                },
            )
        )

    @staticmethod
    def reset_content(connection: Connection, binding_id: str, limit_key: str) -> None:
        table = acquisition.source_failure
        connection.execute(
            sa.update(table)
            .where(
                table.c.source_binding_id == binding_id,
                table.c.limit_key == limit_key,
                table.c.failure_class == "content",
            )
            .values(consecutive_content_errors=0)
        )

    # -- source_config.health ----------------------------------------------------------------
    def _health(self, connection: Connection) -> dict[str, Any]:
        raw = connection.execute(
            sa.select(acquisition.source_config.c.health).where(
                acquisition.source_config.c.plugin_id == self._plugin_id
            )
        ).scalar()
        return dict(raw) if isinstance(raw, dict) else {}

    def _save(self, connection: Connection, health: dict[str, Any]) -> None:
        table = acquisition.source_config
        stmt = sqlite_insert(table).values(plugin_id=self._plugin_id, health=health)
        connection.execute(
            stmt.on_conflict_do_update(index_elements=["plugin_id"], set_={"health": health})
        )

    def paused(self, connection: Connection) -> Pause | None:
        raw = self._health(connection).get("pause")
        if not isinstance(raw, dict):
            return None
        return Pause(raw["failure_class"], raw["event_type"], raw.get("waiting_state"))

    def pause(self, connection: Connection, pause: Pause, now: datetime) -> bool:
        """Start a pause; False when the service was already paused (nothing to announce)."""
        health = self._health(connection)
        if "pause" in health:
            return False
        health["pause"] = {
            "failure_class": pause.failure_class,
            "event_type": pause.event_type,
            "waiting_state": pause.waiting_state,
            "since": now.isoformat(),
        }
        self._save(connection, health)
        return True

    def resume(self, connection: Connection) -> None:
        health = self._health(connection)
        if health.pop("pause", None) is not None:
            self._save(connection, health)

    def record_download(
        self, connection: Connection, limit_key: str, *, ok: bool, now: datetime
    ) -> None:
        """Last good download and the outcomes counted per upstream source (EF-SRC-026 c)."""
        health = self._health(connection)
        entry = health.setdefault("by_source", {}).setdefault(limit_key, {"ok": 0, "failed": 0})
        entry["ok" if ok else "failed"] += 1
        if ok:
            entry["last_ok_at"] = now.isoformat()
        self._save(connection, health)
