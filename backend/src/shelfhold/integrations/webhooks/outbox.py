"""The outbox: what is to be sent, kept as rows so that nothing depends on a process staying alive.

`enqueue` reads the events after each subscription's cursor through `events.integration_feed` (the
activity view of the integration role, each row brought to the current version) and makes one row
per subscription, event and version of the shape to send: the previous version too while its
deprecation is open. The rows and the cursor are written in **one transaction**: a process killed
in between leaves neither, and the next sweep makes them all; the same three can exist once, so a
sweep that runs again makes nothing twice.

A row is **claimed** before it is sent: its attempt is counted and it is held back for a lease. A
process killed while the request is on the wire leaves a row that is due again when the lease ends,
and the receiver recognises the second copy by the delivery identity (the row id) it carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.events.integration_feed import read_after
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import read_transaction, write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import webhooks as schema
from shelfhold.integrations.webhooks.catalogue import WEBHOOK_EVENTS, Catalogue

__all__ = [
    "BASE_DELAY",
    "BATCH",
    "LEASE",
    "MAX_ATTEMPTS",
    "MAX_DELAY",
    "Due",
    "Outbox",
    "retry_delay",
]

BASE_DELAY: Final = 30  # seconds; doubled at each attempt
MAX_DELAY: Final = 6 * 3600
MAX_ATTEMPTS: Final = 8
LEASE: Final = 600  # seconds a claimed row is held back before it is due again
BATCH: Final = 20  # rows claimed at once: BATCH x the longest send (20 s) stays under the lease
PER_SUBSCRIPTION: Final = 5  # of them, at most, for one address: a dead one does not hold the rest
RELEASE_DELAY: Final = 300  # seconds before a row we could not even open is tried again
_READ: Final = 200
_outbox = schema.webhook_outbox
_subscription = schema.webhook_subscription


def retry_delay(attempts: int) -> timedelta:
    """The wait after the `attempts`-th failure: 30 s, 60 s, 120 s, ... up to six hours."""
    return timedelta(seconds=min(BASE_DELAY * 2 ** max(attempts - 1, 0), MAX_DELAY))


@dataclass(frozen=True)
class Due:
    id: str
    subscription_id: str
    event_id: int
    schema_version: int
    attempts: int  # counting this one


class Outbox:
    def __init__(
        self, engine: Engine, clock: Clock, *, catalogue: Catalogue = WEBHOOK_EVENTS
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._catalogue = catalogue

    def enqueue(self) -> int:
        """Make the rows of every event the subscriptions have not seen yet; the number made."""
        with read_transaction(self._engine) as connection:
            subscriptions = (
                connection.execute(
                    sa.select(_subscription.c.id).where(_subscription.c.enabled.is_(True))
                )
                .scalars()
                .all()
            )
        made = 0
        for subscription_id in subscriptions:
            while True:
                step = self._sweep(subscription_id)
                if step is None:
                    break
                made += step
        return made

    def _sweep(self, subscription_id: str) -> int | None:
        """One batch of one subscription in one transaction; None when there is nothing more."""
        with write_transaction(self._engine) as connection:
            row = connection.execute(
                sa.select(_subscription).where(
                    _subscription.c.id == subscription_id, _subscription.c.enabled.is_(True)
                )
            ).first()
            if row is None:
                return None
            events, cursor = read_after(
                connection, row.last_event_id, as_user=row.created_by, limit=_READ,
                require_admin=True,
            )  # fmt: skip
            if cursor == row.last_event_id:
                return None
            now = self._clock.now()
            made = 0
            wanted = set(row.event_types)
            for event in events:
                if event.type not in wanted:
                    continue
                for version in self._catalogue.versions_for(event.type, now):
                    made += self._insert(connection, subscription_id, event.event_id, version, now)
            connection.execute(
                sa.update(_subscription)
                .where(_subscription.c.id == subscription_id)
                .values(last_event_id=cursor)
            )
            return made

    def _insert(
        self, connection: Connection, subscription_id: str, event_id: int, version: int,
        now: datetime,
    ) -> int:  # fmt: skip
        result = connection.execute(
            sqlite_insert(_outbox)
            .values(
                id=new_ulid(),
                subscription_id=subscription_id,
                event_id=event_id,
                schema_version=version,
                next_try_at=now,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["subscription_id", "event_id", "schema_version"]
            )
        )
        return int(result.rowcount)

    def claim(self, limit: int = BATCH) -> list[Due]:
        """The rows due now, claimed: an attempt counted and a lease, in one transaction. At most
        `PER_SUBSCRIPTION` for one address, so that a receiver that does not answer cannot keep
        the others waiting; the lease outlasts the whole batch."""
        now = self._clock.now()
        with write_transaction(self._engine) as connection:
            order = (_outbox.c.next_try_at, _outbox.c.event_id, _outbox.c.schema_version)
            ranked = (
                sa.select(
                    _outbox.c.id, _outbox.c.subscription_id, _outbox.c.event_id,
                    _outbox.c.schema_version, _outbox.c.attempts, _outbox.c.next_try_at,
                    sa.func.row_number().over(
                        partition_by=_outbox.c.subscription_id, order_by=order
                    ).label("rank"),
                )
                .join(_subscription, _subscription.c.id == _outbox.c.subscription_id)
                .where(_outbox.c.status == "pending", _outbox.c.next_try_at <= now,
                       _subscription.c.enabled.is_(True))
                .subquery()
            )  # fmt: skip
            rows = connection.execute(
                sa.select(ranked)
                .where(ranked.c.rank <= PER_SUBSCRIPTION)
                .order_by(ranked.c.next_try_at, ranked.c.event_id, ranked.c.schema_version)
                .limit(limit)
            ).all()
            for row in rows:
                connection.execute(
                    sa.update(_outbox).where(_outbox.c.id == row.id).values(
                        attempts=row.attempts + 1, next_try_at=now + timedelta(seconds=LEASE)
                    )
                )  # fmt: skip
        return [Due(r.id, r.subscription_id, r.event_id, r.schema_version, r.attempts + 1)
                for r in rows]  # fmt: skip

    def delivered(self, row: Due, status: int) -> None:
        self._settle(row, status="delivered", last_status=status, last_error=None,
                     delivered_at=self._clock.now())  # fmt: skip

    def failed(
        self, row: Due, *, status: int | None, error: str | None, final: bool = False
    ) -> bool:
        """Record a failed attempt: due again after the delay, or dead after the last one (or at
        once when `final`). True when the row is dead."""
        dead = final or row.attempts >= MAX_ATTEMPTS
        values: dict[str, Any] = {"last_status": status, "last_error": error}
        if dead:
            self._settle(row, status="dead", **values)
        else:
            retry_at = self._clock.now() + retry_delay(row.attempts)
            self._settle(row, status="pending", next_try_at=retry_at, **values)
        return dead

    def release(self, row: Due) -> None:
        """Give the claim back without counting the attempt: what failed was ours (the database,
        the keyring), not the receiver's."""
        self._settle(
            row, attempts=row.attempts - 1,
            next_try_at=self._clock.now() + timedelta(seconds=RELEASE_DELAY),
        )  # fmt: skip

    def _settle(self, row: Due, **values: Any) -> None:
        """Only the attempt that holds the claim settles the row: a slow copy that comes back after
        the lease ran out (another already delivered or retried it) changes nothing."""
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.update(_outbox)
                .where(
                    _outbox.c.id == row.id,
                    _outbox.c.status == "pending",
                    _outbox.c.attempts == row.attempts,
                )
                .values(**values)
            )
