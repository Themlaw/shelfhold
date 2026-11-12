"""The maintenance mode of the instance (EF-INT-018): one persisted flag that freezes the writes.

While `instance.maintenance_since` is set, nothing starts on the instance's behalf (the writes of
recovery at start-up, which settle what a crash left open, are not work and go on):

- every HTTP request that is not a read is refused (`maintenance.active`, 503), reserved routes
  included; what stays open is reading, `/healthz`, the sign-in that a fresh session needs, what
  takes an access away (sign-out, a revoked session, key or passkey) and the exit itself;
- the scheduler claims nothing: what falls due waits in `schedule` and is caught up once, after the
  exit (run-once, as after an outage);
- a job that reaches a worker, queued before or after the entry, is parked under `PARK_CODE` (no
  attempt spent, nothing failed); the exit puts them back in the queue;
- every command of the host CLI that is not a read, a revocation or the exit is refused as well, by
  one gate (`interfaces.cli.registry`), and the socket refuses its writes the same way.

The flag is the database's, so it survives a restart: the server that comes back is still frozen.
A job that was running when the mode was entered finishes (nothing interrupts a step, ENF-OPS-007);
`running_jobs` says how many were still draining. Entering and leaving are reserved actions
(`maintenance.enter`, `maintenance.exit`): the audit line is written in the transaction of the
change, by `ops.maintenance`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import platform

__all__ = ["ACTIVE_CODE", "PARK_CODE", "MaintenanceMode", "MaintenanceState"]

ACTIVE_CODE: Final = "maintenance.active"  # the catalog code of the refusal (HTTP 503)
PARK_CODE: Final = "maintenance"  # `job_view.error_code` of a job parked by the mode

instance = platform.instance


@dataclass(frozen=True)
class MaintenanceState:
    active: bool
    since: datetime | None = None
    by: str | None = None  # the user that entered it (None: the host CLI)


class MaintenanceMode:
    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    def state(self, connection: Connection | None = None) -> MaintenanceState:
        if connection is None:
            with self._engine.connect() as own:
                return self.state(own)
        row = connection.execute(
            sa.select(instance.c.maintenance_since, instance.c.maintenance_by)
        ).first()
        if row is None or row.maintenance_since is None:
            return MaintenanceState(False)
        return MaintenanceState(True, row.maintenance_since, row.maintenance_by)

    def probe(self) -> bool | None:
        """Read from the database at every call, never cached: the flag may be set by another
        process (the host CLI of a stopped server). None when the database could not be read
        (locked, busy): that is not the mode, and must not be told as if it were."""
        try:
            return self.state().active
        except sa.exc.OperationalError:
            return None

    def active(self) -> bool:
        """`probe`, on the safe side: a database that cannot be read counts as active for whoever
        only needs to know whether a write may go (it could not have succeeded anyway)."""
        return self.probe() is not False

    def enter(self, connection: Connection, *, by: str | None) -> bool:
        """In the caller's transaction. False when the mode was already on (the first entry keeps
        its date and its author)."""
        done = connection.execute(
            sa.update(instance)
            .where(instance.c.maintenance_since.is_(None))
            .values(maintenance_since=self._clock.now(), maintenance_by=by)
        ).rowcount
        if not done and connection.execute(sa.select(instance.c.instance_id)).first() is None:
            raise LookupError("the instance row does not exist yet")
        return bool(done)

    def leave(self, connection: Connection) -> bool:
        """In the caller's transaction. False when the mode was not on."""
        return bool(
            connection.execute(
                sa.update(instance)
                .where(instance.c.maintenance_since.is_not(None))
                .values(maintenance_since=None, maintenance_by=None)
            ).rowcount
        )
