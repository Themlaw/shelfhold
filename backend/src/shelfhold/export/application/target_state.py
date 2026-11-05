"""What the core keeps about a destination between two calls, in the database.

The health of the target (`export_target.health`), the fingerprint of its instance
(`kavita_instance`), the `field_owner` registry, the pause of a shelf and the cache of remote ids.
Plain SQL on the tables of `infrastructure.schema.export`; no network, no Kavita type.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.export.application.target_health import TargetHealth
from shelfhold.export.domain.field_owner import OwnedField
from shelfhold.infrastructure.schema import export

__all__ = [
    "DestinationPausedError",
    "FieldRegistry",
    "ensure_active",
    "forget_remote_cache",
    "invalidate_field_owners",
    "load_fingerprint",
    "load_health",
    "pause_shelf",
    "resume_shelf",
    "save_fingerprint",
    "save_health",
    "target_write_guard",
]

CHILD_EXPOSURE = "child_exposure"


class DestinationPausedError(Exception):
    """A destination waits for a human (reinstalled server, child exposure, 403): nothing runs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# -- health --------------------------------------------------------------------------------------


def load_health(connection: Connection, target_id: str) -> TargetHealth:
    raw = connection.execute(
        sa.select(export.export_target.c.health).where(export.export_target.c.id == target_id)
    ).scalar_one()
    return TargetHealth.from_json(raw) if raw else TargetHealth()


def save_health(connection: Connection, target_id: str, health: TargetHealth) -> None:
    connection.execute(
        sa.update(export.export_target)
        .where(export.export_target.c.id == target_id)
        .values(health=health.to_json(), version_seen=health.version)
    )


# -- instance fingerprint ------------------------------------------------------------------------


def load_fingerprint(connection: Connection, target_id: str) -> str | None:
    return connection.execute(
        sa.select(export.kavita_instance.c.fingerprint).where(
            export.kavita_instance.c.target_id == target_id
        )
    ).scalar()


def save_fingerprint(
    connection: Connection, target_id: str, fingerprint: str, version: str | None, now: datetime
) -> None:
    t = export.kavita_instance
    values = {"fingerprint": fingerprint, "version": version, "seen_at": now}
    updated = connection.execute(sa.update(t).where(t.c.target_id == target_id).values(**values))
    if updated.rowcount == 0:
        connection.execute(sa.insert(t).values(target_id=target_id, **values))


# -- pause ---------------------------------------------------------------------------------------


def pause_shelf(connection: Connection, shelf_id: str, reason: str) -> bool:
    """Pause a shelf; False when it was already paused for this reason."""
    s = export.shelf
    done = connection.execute(
        sa.update(s)
        .where(s.c.id == shelf_id, sa.or_(s.c.paused_reason.is_(None), s.c.paused_reason != reason))
        .values(paused_reason=reason)
    )
    return done.rowcount > 0


def resume_shelf(connection: Connection, shelf_id: str, reason: str) -> bool:
    s = export.shelf
    done = connection.execute(
        sa.update(s)
        .where(s.c.id == shelf_id, s.c.paused_reason == reason)
        .values(paused_reason=None)
    )
    return done.rowcount > 0


def ensure_active(connection: Connection, destination_id: str) -> None:
    """Refuse to write for a paused shelf or a target that waits for a human."""
    d, s, t = export.shelf_destination, export.shelf, export.export_target
    row = connection.execute(
        sa.select(s.c.paused_reason, t.c.health, d.c.target_id)
        .select_from(d.join(s, s.c.id == d.c.shelf_id).join(t, t.c.id == d.c.target_id))
        .where(d.c.id == destination_id)
    ).one()
    if row.paused_reason:
        raise DestinationPausedError(f"target.{row.paused_reason}")
    health = TargetHealth.from_json(row.health) if row.health else TargetHealth()
    if health.instance_changed:
        raise DestinationPausedError("target.instance_changed")
    if health.paused:
        raise DestinationPausedError("target.forbidden")


def target_write_guard(engine: Engine, target_id: str) -> Callable[[], None]:
    """For `KavitaClient.set_write_guard`: no write route while a shelf of the target is paused
    or the target waits for a human."""

    def guard() -> None:
        with engine.connect() as connection:
            paused = connection.execute(
                sa.select(export.shelf.c.paused_reason)
                .select_from(
                    export.shelf_destination.join(
                        export.shelf, export.shelf.c.id == export.shelf_destination.c.shelf_id
                    )
                )
                .where(
                    export.shelf_destination.c.target_id == target_id,
                    export.shelf.c.paused_reason.is_not(None),
                )
            ).scalar()
            if paused:
                raise DestinationPausedError(f"target.{paused}")
            health = load_health(connection, target_id)
        if health.instance_changed:
            raise DestinationPausedError("target.instance_changed")
        if health.paused:
            raise DestinationPausedError("target.forbidden")

    return guard


# -- remote ids are a cache ----------------------------------------------------------------------


def forget_remote_cache(connection: Connection, target_id: str) -> None:
    """Forget what the old server said (series ids, remote references): the paths stay.

    `export_record.rel_path` is the key; nothing here touches a file or a record's state.
    """
    destinations = sa.select(export.shelf_destination.c.id).where(
        export.shelf_destination.c.target_id == target_id
    )
    connection.execute(
        sa.update(export.export_record)
        .where(export.export_record.c.shelf_destination_id.in_(destinations))
        .values(remote_ref=None)
    )
    connection.execute(
        sa.update(export.export_series)
        .where(export.export_series.c.shelf_destination_id.in_(destinations))
        .values(kavita_series_id=None)
    )


def invalidate_field_owners(connection: Connection, target_id: str) -> int:
    """The registry of the old instance no longer says what the new one holds: push again."""
    destinations = sa.select(export.shelf_destination.c.id).where(
        export.shelf_destination.c.target_id == target_id
    )
    done = connection.execute(
        sa.update(export.field_owner)
        .where(export.field_owner.c.shelf_destination_id.in_(destinations))
        .values(pushed_at=None)
    )
    return int(done.rowcount)


# -- the `field_owner` registry --------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRegistry:
    """The fields Shelfhold itself locked in Kavita (EF-EXP-057): only these are rewritten."""

    connection: Connection

    def get(self, destination_id: str, series_id: str, field: str) -> OwnedField | None:
        row = self.connection.execute(self._where(destination_id, series_id, field)).first()
        return None if row is None else OwnedField(row.last_value, row.pushed_at is not None)

    def record_push(
        self, destination_id: str, series_id: str, field: str, value: Any, now: datetime
    ) -> None:
        t = export.field_owner
        done = self.connection.execute(
            sa.update(t)
            .where(*self._keys(destination_id, series_id, field))
            .values(last_value=value, pushed_at=now)
        )
        if done.rowcount == 0:
            self.connection.execute(
                sa.insert(t).values(
                    shelf_destination_id=destination_id, export_series_id=series_id, field=field,
                    last_value=value, pushed_at=now,
                )
            )  # fmt: skip

    @staticmethod
    def _keys(destination_id: str, series_id: str, field: str) -> tuple[Any, ...]:
        t = export.field_owner
        return (
            t.c.shelf_destination_id == destination_id,
            t.c.export_series_id == series_id,
            t.c.field == field,
        )

    def _where(self, destination_id: str, series_id: str, field: str) -> sa.Select[Any]:
        return sa.select(export.field_owner).where(*self._keys(destination_id, series_id, field))
