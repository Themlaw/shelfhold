"""Per-domain concurrency slots in `domain_slot` (EF-INT-003, ENF-OPS-007).

`try_acquire` is one conditional UPDATE, so two workers cannot both take the last slot. A holder
that died keeps its slot until the lease runs out (and boot releases every lease); the lease is
renewed by each acquisition. A worker never waits for a slot: it asks again later (the job is
rescheduled by `huey_app`), so a busy domain does not hold a thread.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import platform

domain_slot = platform.domain_slot
DEFAULT_LEASE = timedelta(minutes=10)
RETRY_DELAY_S = 30  # how long a job whose domain is full waits before asking again


class UnknownLimitKeyError(KeyError):
    """No slot count was declared for this limit key."""


class SlotBusyError(Exception):
    """Every slot of the domain is in use."""


class DomainSlots:
    def __init__(self, engine: Engine, clock: Clock, *, lease: timedelta = DEFAULT_LEASE) -> None:
        self._engine = engine
        self._clock = clock
        self._lease = lease

    def declare(self, limit_key: str, slots: int) -> None:
        """Create the key or change its cap; leases already held are kept."""
        stmt = sqlite_insert(domain_slot).values(limit_key=limit_key, slots=slots, in_use=0)
        stmt = stmt.on_conflict_do_update(
            index_elements=["limit_key"],
            set_={"slots": slots, "in_use": sa.func.min(domain_slot.c.in_use, slots)},
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def try_acquire(self, limit_key: str) -> bool:
        now = self._clock.now()
        with self._engine.begin() as conn:
            conn.execute(  # a lease nobody renewed is a dead holder: free the key
                sa.update(domain_slot)
                .where(domain_slot.c.limit_key == limit_key, domain_slot.c.lease_until < now)
                .values(in_use=0, lease_until=None)
            )
            taken = conn.execute(
                sa.update(domain_slot)
                .where(
                    domain_slot.c.limit_key == limit_key, domain_slot.c.in_use < domain_slot.c.slots
                )
                .values(in_use=domain_slot.c.in_use + 1, lease_until=now + self._lease)
            ).rowcount
            if taken:
                return True
            known = conn.execute(
                sa.select(domain_slot.c.limit_key).where(domain_slot.c.limit_key == limit_key)
            ).first()
        if known is None:
            raise UnknownLimitKeyError(limit_key)
        return False

    def release(self, limit_key: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(domain_slot)
                .where(domain_slot.c.limit_key == limit_key, domain_slot.c.in_use > 0)
                .values(in_use=domain_slot.c.in_use - 1)
            )

    def release_all(self) -> int:
        """At boot, once the instance lock is ours: no worker of a previous run is alive."""
        with self._engine.begin() as conn:
            leased = conn.execute(
                sa.select(sa.func.coalesce(sa.func.sum(domain_slot.c.in_use), 0))
            ).scalar_one()
            conn.execute(sa.update(domain_slot).values(in_use=0, lease_until=None))
        return int(leased)

    def in_use(self, limit_key: str) -> int:
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    sa.select(domain_slot.c.in_use).where(domain_slot.c.limit_key == limit_key)
                ).scalar_one()
            )

    @contextmanager
    def slot(self, limit_key: str) -> Iterator[None]:
        if not self.try_acquire(limit_key):
            raise SlotBusyError(limit_key)
        try:
            yield
        finally:
            self.release(limit_key)
