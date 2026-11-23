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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.events.integration_feed import read_after, read_type
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
    "RECENT",
    "DeadDeliveryPublisher",
    "Delivery",
    "Due",
    "Outbox",
    "recent",
    "retry_delay",
]

BASE_DELAY: Final = 30  # seconds; doubled at each attempt
MAX_DELAY: Final = 6 * 3600
MAX_ATTEMPTS: Final = 8
LEASE: Final = 600  # seconds a claimed row is held back before it is due again
BATCH: Final = 20  # rows claimed at once: BATCH x the longest send (20 s) stays under the lease
PER_SUBSCRIPTION: Final = 5  # of them, at most, for one address: a dead one does not hold the rest
RELEASE_DELAY: Final = 300  # seconds before a row we could not even open is tried again
RECENT: Final = 20  # the last deliveries the screen of the webhooks shows for one subscription
_READ: Final = 200
_outbox = schema.webhook_outbox
_subscription = schema.webhook_subscription

log = structlog.get_logger(__name__)


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


@dataclass(frozen=True)
class Delivery:
    """One row of the outbox as the screen reads it (M6 L23b): what became of one event sent to one
    address. The identity is the receiver's idempotency key; nothing of the address, of its path or
    of the secret is here."""

    id: str
    event_id: int
    schema_version: int
    status: str
    attempts: int
    last_status: int | None
    last_error: str | None
    delivered_at: datetime | None
    next_try_at: datetime


def recent(
    connection: Connection, subscription_id: str, *, limit: int = RECENT
) -> list[Delivery] | None:
    """The last deliveries of one subscription, newest first (the order of the events, then of the
    versions of their shape); None when the subscription is not there. The rows only: the sealed
    document is not opened, so an address that cannot be read still shows what was sent to it."""
    known = connection.execute(
        sa.select(_subscription.c.id).where(_subscription.c.id == subscription_id)
    ).first()
    if known is None:
        return None
    rows = connection.execute(
        sa.select(_outbox)
        .where(_outbox.c.subscription_id == subscription_id)
        .order_by(_outbox.c.event_id.desc(), _outbox.c.schema_version.desc(), _outbox.c.id.desc())
        .limit(limit)
    ).all()
    return [
        Delivery(
            id=row.id,
            event_id=row.event_id,
            schema_version=row.schema_version,
            status=row.status,
            attempts=row.attempts,
            last_status=row.last_status,
            last_error=row.last_error,
            delivered_at=row.delivered_at,
            next_try_at=row.next_try_at,
        )
        for row in rows
    ]


class DeadDeliveryPublisher(Protocol):
    """What the outbox tells when a delivery is given up: the event bus, injected so this package
    never imports it (the import contract keeps `events.bus` out of `integrations.webhooks`)."""

    def publish(
        self, connection: Connection, event_type: str, payload: Mapping[str, object]
    ) -> object: ...


class Outbox:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        *,
        catalogue: Catalogue = WEBHOOK_EVENTS,
        publisher: DeadDeliveryPublisher | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._catalogue = catalogue
        self._publisher = publisher

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
        self, row: Due, *, status: int | None, error: str | None, final: bool = False,
        reason: str | None = None,
    ) -> bool:  # fmt: skip
        """Record a failed attempt: due again after the delay, or dead after the last one (or at
        once when `final`). True when the row is dead. `reason` names the announcement of the
        death (`event_gone`, `not_theirs`); without one it is `retries_exhausted`, or
        `event_gone` for a final failure."""
        if final or row.attempts >= MAX_ATTEMPTS:
            self._die(row, status=status, error=error, final=final, reason=reason)
            return True
        retry_at = self._clock.now() + retry_delay(row.attempts)
        self._settle(
            row, status="pending", next_try_at=retry_at, last_status=status, last_error=error
        )
        return False

    def _die(
        self, row: Due, *, status: int | None, error: str | None, final: bool,
        reason: str | None = None,
    ) -> None:  # fmt: skip
        """Settle the row as dead and announce it once (M6 L23a): the `webhook.dead` event is
        written in the same transaction, and only by the attempt that holds the claim — a slow
        copy that comes back after the lease changed nothing and announces nothing.

        A dead delivery of a `webhook.dead` announces nothing (M6 L23a review): the announcement
        would be enqueued to the same broken receiver, die there, and announce again — a loop
        that doubles with every turn. The row is settled either way."""
        with write_transaction(self._engine) as connection:
            settled = connection.execute(
                sa.update(_outbox)
                .where(
                    _outbox.c.id == row.id,
                    _outbox.c.status == "pending",
                    _outbox.c.attempts == row.attempts,
                )
                .values(status="dead", last_status=status, last_error=error)
            )
            if settled.rowcount != 1 or self._publisher is None:
                return
            if read_type(connection, row.event_id) == "webhook.dead":
                log.info("webhook.dead_unannounced", event_id=row.event_id)
                return
            self._publisher.publish(
                connection, "webhook.dead",
                {"subscription_id": row.subscription_id, "event_id": row.event_id,
                 "reason": reason or ("event_gone" if final else "retries_exhausted")},
            )  # fmt: skip

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
