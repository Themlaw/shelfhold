"""Resource locks (EF-EXP-042, I10): one disk-modifying job per Work and series.

A job that modifies the disk holds `work:<id>` then `path:<destination>/<series folder>`, always in
that order and both or none, so two jobs cannot deadlock. Huey's `lock_task` only serves
idempotence. A lock carries a lease: a killed job's rows run out, and `release_all` frees them at
boot once the instance lock is ours.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import coherence

DEFAULT_LEASE = timedelta(minutes=10)
POLL_SECONDS = 0.02

__all__ = ["DEFAULT_LEASE", "LockBusyError", "ResourceLocks", "path_resource", "work_resource"]


class LockBusyError(Exception):
    def __init__(self, resource: str) -> None:
        super().__init__(f"{resource} is held by another job")
        self.resource = resource


def work_resource(work_id: str) -> str:
    return f"work:{work_id}"


def path_resource(series: str) -> str:
    return f"path:{series}"


class ResourceLocks:
    def __init__(self, engine: Engine, *, clock: Clock, lease: timedelta = DEFAULT_LEASE) -> None:
        self._engine = engine
        self._clock = clock
        self._lease = lease

    @contextmanager
    def hold(self, job_id: str, *, work: str, series: str, timeout: float = 0.0) -> Iterator[None]:
        resources = [work_resource(work), path_resource(series)]  # the fixed order
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._try_acquire(job_id, resources)
                break
            except LockBusyError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(POLL_SECONDS)
        try:
            yield
        finally:
            self._release(job_id, resources)

    def _try_acquire(self, job_id: str, resources: list[str]) -> None:
        table = coherence.resource_lock
        now = self._clock.now()
        try:
            with self._engine.begin() as conn:
                # The DELETE takes SQLite's write lock: what follows is serialised.
                conn.execute(sa.delete(table).where(table.c.lease_until <= now))
                for resource in resources:
                    held = conn.execute(
                        sa.select(table.c.holder_job_id).where(table.c.resource == resource)
                    ).first()
                    if held is not None:
                        raise LockBusyError(resource)
                conn.execute(
                    sa.insert(table),
                    [
                        {"resource": r, "holder_job_id": job_id, "lease_until": now + self._lease}
                        for r in resources
                    ],
                )
        except IntegrityError:
            raise LockBusyError(resources[0]) from None

    def _release(self, job_id: str, resources: list[str]) -> None:
        table = coherence.resource_lock
        with self._engine.begin() as conn:
            conn.execute(
                sa.delete(table).where(
                    table.c.resource.in_(resources), table.c.holder_job_id == job_id
                )
            )

    def release_all(self) -> int:
        """At boot, once the instance lock is ours: whatever a killed process left."""
        with self._engine.begin() as conn:
            return conn.execute(sa.delete(coherence.resource_lock)).rowcount
