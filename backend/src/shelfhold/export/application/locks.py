"""Resource locks (EF-EXP-042, I10): one disk-modifying job per Work and series.

A job that modifies the disk holds `work:<id>` then `path:<destination>/<series folder>`, always in
that order and both or none, so two jobs cannot deadlock. Huey's `lock_task` only serves
idempotence. A lock carries a lease: a killed job's rows run out, and `release_all` frees them at
boot once the instance lock is ours.
"""

import threading
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

__all__ = [
    "DEFAULT_LEASE",
    "HeldLocks",
    "LockBusyError",
    "LockLostError",
    "ResourceLocks",
    "library_resource",
    "path_resource",
    "work_resource",
]


class LockBusyError(Exception):
    def __init__(self, resource: str) -> None:
        super().__init__(f"{resource} is held by another job")
        self.resource = resource


class LockLostError(Exception):
    """The job's lock could not be renewed (it ran out and may belong to another job): the job
    stops before its next write."""

    code = "lock.lost"


def work_resource(work_id: str) -> str:
    return f"work:{work_id}"


def path_resource(series: str) -> str:
    return f"path:{series}"


def library_resource(remote_library_id: str, normalized_name: str) -> str:
    """A series name of a remote library, whichever shelf writes it (EF-EXP-062)."""
    return f"library:{remote_library_id}/{normalized_name}"


class HeldLocks:
    """The locks of one job: renewed by a heartbeat, `ensure()` before each write."""

    def __init__(self, owner: "ResourceLocks", job_id: str, resources: list[str]) -> None:
        self._owner = owner
        self.job_id = job_id
        self.resources = resources
        self._lost = threading.Event()

    def renew(self) -> None:
        """Extend `lease_until`, only for rows still held by this job; fewer rows = lost."""
        if self._owner._renew(self.job_id, self.resources) != len(self.resources):
            self._lost.set()
            raise LockLostError(f"{self.resources}: no longer held by job {self.job_id}")

    def ensure(self) -> None:
        """Raise `LockLostError` if the locks were lost (checked against the table, not memory)."""
        if self._lost.is_set() or not self._owner._still_held(self.job_id, self.resources):
            self._lost.set()
            raise LockLostError(f"{self.resources}: no longer held by job {self.job_id}")


class ResourceLocks:
    def __init__(self, engine: Engine, *, clock: Clock, lease: timedelta = DEFAULT_LEASE) -> None:
        self._engine = engine
        self._clock = clock
        self._lease = lease

    @contextmanager
    def hold(
        self,
        job_id: str,
        *,
        work: str,
        series: str,
        library: str | None = None,
        timeout: float = 0.0,
        heartbeat: float | None = None,
    ) -> Iterator[HeldLocks]:
        """Both locks or none. A heartbeat thread renews them every `heartbeat` seconds (a third
        of the lease by default); a job that cannot renew finds out at `ensure()`."""
        resources = [work_resource(work), path_resource(series)]  # the fixed order
        if library is not None:
            resources.append(library)  # always last: `work`, `path`, then `library`
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._try_acquire(job_id, resources)
                break
            except LockBusyError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(POLL_SECONDS)
        held = HeldLocks(self, job_id, resources)
        stop = threading.Event()
        interval = heartbeat if heartbeat is not None else self._lease.total_seconds() / 3

        def beat() -> None:
            while not stop.wait(interval):
                try:
                    held.renew()
                except LockLostError:
                    return

        thread = threading.Thread(target=beat, name=f"lock-heartbeat-{job_id}", daemon=True)
        thread.start()
        try:
            yield held
        finally:
            stop.set()
            thread.join()
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

    def _renew(self, job_id: str, resources: list[str]) -> int:
        table = coherence.resource_lock
        with self._engine.begin() as conn:
            return conn.execute(
                sa.update(table)
                .where(table.c.resource.in_(resources), table.c.holder_job_id == job_id)
                .values(lease_until=self._clock.now() + self._lease)
            ).rowcount

    def _still_held(self, job_id: str, resources: list[str]) -> bool:
        table = coherence.resource_lock
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(table.c.resource, table.c.lease_until).where(
                    table.c.resource.in_(resources), table.c.holder_job_id == job_id
                )
            ).all()
        now = self._clock.now()
        return len(rows) == len(resources) and all(r.lease_until > now for r in rows)

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
