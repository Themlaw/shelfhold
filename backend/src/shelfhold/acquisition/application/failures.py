"""What a failure does to the database: the content counter, the service pause, source health.

Only `content` is counted in `source_failure`. A pause lives in `source_config.health` (one row per
plugin), is announced once when it starts, and ends with `resume` (the admin renewed the secret,
updated the tool...). Nothing here ever touches `source_config.consecutive_failures`.
"""

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.events.waiting_state import WaitingState
from shelfhold.infrastructure.fetcher.errors import InvalidFetchUrl
from shelfhold.infrastructure.schema import acquisition
from shelfhold.sdk.source import SourceChanged

_PAUSE_EVENTS = {
    "auth": "source.auth_expired",
    "version": "source.version_unsupported",
    "schema": "source.version_unsupported",  # the upstream schema moved: a version problem
    "instance": "source.instance_changed",  # the tool was installed again (EF-SRC-036)
}


# What an adapter raises when an answer is not shaped as it expects: the upstream schema moved.
_SHAPE_ERRORS = (KeyError, TypeError, IndexError, AttributeError)


def unexpected_shape(error: BaseException) -> SourceChanged:
    return SourceChanged(f"unexpected answer from the service ({type(error).__name__})")


async def from_service[T](call: Awaitable[T]) -> T:
    """Await a call to the connected service and read its failures as the core must.

    Only here is a malformed answer a schema change (pause): the adapter is the one that reads the
    wire. A URL the plugin hands back that the Fetcher refuses is the plugin changing too
    (`SourceChanged`); the same error from Shelfhold's own code is not.
    """
    try:
        return await call
    except _SHAPE_ERRORS as error:
        raise unexpected_shape(error) from error
    except InvalidFetchUrl as error:
        raise SourceChanged("the service handed back a URL the Fetcher refuses") from error


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


_SET = sa.text(
    "UPDATE source_config SET health = json_set(coalesce(health, '{}'), :path, json(:value)) "
    "WHERE plugin_id = :plugin"
)
_SET_IF_UNSET = sa.text(
    "UPDATE source_config SET health = json_set(coalesce(health, '{}'), :path, json(:value)) "
    "WHERE plugin_id = :plugin AND json_extract(coalesce(health, '{}'), :path) IS NULL"
)
_COUNT_OK = sa.text(
    "UPDATE source_config SET health = json_set(coalesce(health, '{}'), :path, "
    "coalesce(json_extract(coalesce(health, '{}'), :path), 0) + 1, :last, :now) "
    "WHERE plugin_id = :plugin"
)
_COUNT_FAILED = sa.text(
    "UPDATE source_config SET health = json_set(coalesce(health, '{}'), :path, "
    "coalesce(json_extract(coalesce(health, '{}'), :path), 0) + 1) WHERE plugin_id = :plugin"
)


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
    # Every write is ONE statement on the JSON document (json_set / json_remove), so two writers
    # (a cycle and the admin's `resume`, two instances of a job) never overwrite each other's key.
    def _ensure(self, connection: Connection) -> None:
        connection.execute(
            sqlite_insert(acquisition.source_config)
            .values(plugin_id=self._plugin_id, health={})
            .on_conflict_do_nothing(index_elements=["plugin_id"])
        )

    def _read(self, connection: Connection) -> dict[str, Any]:
        raw = connection.execute(
            sa.select(acquisition.source_config.c.health).where(
                acquisition.source_config.c.plugin_id == self._plugin_id
            )
        ).scalar()
        return dict(raw) if isinstance(raw, dict) else {}

    def _set(
        self, connection: Connection, path: str, value: object, *, if_unset: bool = False
    ) -> bool:
        """Set `path` of the health document; True if a row changed (`if_unset`: only if absent)."""
        self._ensure(connection)
        result = connection.execute(
            _SET_IF_UNSET if if_unset else _SET,
            {"path": path, "value": json.dumps(value), "plugin": self._plugin_id},
        )
        return result.rowcount > 0

    def _unset(self, connection: Connection, path: str) -> bool:
        result = connection.execute(
            sa.text(
                "UPDATE source_config SET health = json_remove(coalesce(health, '{}'), :path) "
                "WHERE plugin_id = :plugin AND json_extract(coalesce(health, '{}'), :path) "
                "IS NOT NULL"
            ),
            {"path": path, "plugin": self._plugin_id},
        )
        return result.rowcount > 0

    def paused(self, connection: Connection) -> Pause | None:
        raw = self._read(connection).get("pause")
        if not isinstance(raw, dict):
            return None
        return Pause(raw["failure_class"], raw["event_type"], raw.get("waiting_state"))

    def pause(self, connection: Connection, pause: Pause, now: datetime) -> bool:
        """Start a pause; False when the service was already paused (nothing to announce)."""
        value = {
            "failure_class": pause.failure_class,
            "event_type": pause.event_type,
            "waiting_state": pause.waiting_state,
            "since": now.isoformat(),
        }
        return self._set(connection, "$.pause", value, if_unset=True)

    def resume(self, connection: Connection) -> bool:
        return self._unset(connection, "$.pause")

    # -- outage: the service does not answer at all (matrix, "Suwayomi injoignable") ------------
    def bump_outage(self, connection: Connection) -> int:
        """One more cycle in a row that could not reach the service; returns the count."""
        self._ensure(connection)
        connection.execute(_COUNT_FAILED, {"path": "$.outage", "plugin": self._plugin_id})
        return int(self._read(connection).get("outage", 0))

    def mark_unavailable(self, connection: Connection) -> bool:
        """True the first time of an episode: the one notification is due."""
        return self._set(connection, "$.unavailable", True, if_unset=True)

    def unavailable(self, connection: Connection) -> bool:
        return bool(self._read(connection).get("unavailable"))

    def clear_outage(self, connection: Connection) -> None:
        self._unset(connection, "$.outage")
        self._unset(connection, "$.unavailable")

    # -- absence from the service's library (EF-SRC-027) ---------------------------------------
    def bump_missing(self, connection: Connection, binding_id: str) -> int:
        """One more consecutive cycle in which the binding's series is absent; returns the count."""
        path = f"$.missing.{json.dumps(binding_id)}"
        self._ensure(connection)
        connection.execute(_COUNT_FAILED, {"path": path, "plugin": self._plugin_id})
        return int(
            connection.execute(
                sa.text(
                    "SELECT json_extract(health, :path) FROM source_config WHERE plugin_id = :p"
                ),
                {"path": path, "p": self._plugin_id},
            ).scalar_one()
        )

    def clear_missing(self, connection: Connection, binding_id: str) -> None:
        self._unset(connection, f"$.missing.{json.dumps(binding_id)}")

    def raise_mass_alert(self, connection: Connection) -> bool:
        """True the first time (the alert is sent once per episode)."""
        return self._set(connection, "$.mass_absence", True, if_unset=True)

    def clear_mass_alert(self, connection: Connection) -> None:
        self._unset(connection, "$.mass_absence")

    # -- unloaded upstream sources (EF-SRC-026 a): told once per episode ---
    def not_loaded(self, connection: Connection) -> frozenset[str]:
        raw = self._read(connection).get("not_loaded")
        return frozenset(raw) if isinstance(raw, dict) else frozenset()

    def mark_not_loaded(self, connection: Connection, source_key: str) -> bool:
        """True the first time of an episode: the one `source.not_loaded` is due."""
        return self._set(connection, f"$.not_loaded.{json.dumps(source_key)}", True, if_unset=True)

    def clear_not_loaded(self, connection: Connection, source_key: str) -> None:
        self._unset(connection, f"$.not_loaded.{json.dumps(source_key)}")

    # -- series silent for too long (EF-SRC-026 c): the episode of each ---
    def sync_stale(self, connection: Connection, current: list[str]) -> list[str]:
        """Remember which editions are stale now; the ones that were not before (a new episode:
        the series spoke again in between, or was never announced), in the order given."""
        held = self._read(connection).get("stale")
        known = set(held) if isinstance(held, list) else set()
        self._set(connection, "$.stale", sorted(current))
        return [edition for edition in current if edition not in known]

    # -- pace (per upstream source, survives a cycle) -------------------------------------------
    def next_at(self, connection: Connection) -> dict[str, float]:
        raw = self._read(connection).get("next_at")
        return {str(k): float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def set_next_at(self, connection: Connection, limit_key: str, epoch: float) -> None:
        self._set(connection, f"$.next_at.{json.dumps(limit_key)}", epoch)

    def record_download(
        self, connection: Connection, limit_key: str, *, ok: bool, now: datetime
    ) -> None:
        """Last good download and the outcomes counted per upstream source (EF-SRC-026 c)."""
        self._ensure(connection)
        base = f"$.by_source.{json.dumps(limit_key)}"
        counter = "ok" if ok else "failed"
        connection.execute(
            _COUNT_OK if ok else _COUNT_FAILED,
            {
                "path": f"{base}.{counter}",
                "last": f"{base}.last_ok_at",
                "now": now.isoformat(),
                "plugin": self._plugin_id,
            },
        )
