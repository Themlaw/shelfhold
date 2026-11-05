"""Graceful shutdown on SIGTERM (ENF-OPS-007), in three steps.

1. stop taking tasks (no job is claimed, the periodic scan queues nothing);
2. let the jobs in flight reach a checkpoint, for at most `checkpoint_s`; one that does not is
   abandoned. Its file-operation intent stays open on purpose: the next start settles it
   (replay or cancel) before any job runs (EF-EXP-041), and its `running` row is requeued;
3. free the locks: Huey's, the domain slots, the resource locks, then the instance lock.

Nothing is deleted and no intent is closed here. The instance lock goes last, so a replacement
instance never starts while this one can still write.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import structlog
from sqlalchemy.exc import OperationalError

from shelfhold.jobs.huey_app import JobPools

DEFAULT_CHECKPOINT_S = 25.0  # under the 30 s a container runtime grants after SIGTERM


class ShuttingDownError(OSError):
    """A write refused because the shutdown went past its checkpoint delay (ENF-OPS-007)."""

    code = "instance.shutting_down"

    def __init__(self) -> None:
        super().__init__(108, "the instance is shutting down: no write after the checkpoint")


class WriteBarrier:
    """Closed once the delay for checkpoints is over: a job that was abandoned keeps running in
    its thread, but every `Fsops` write it tries then fails, so nothing is written once the
    locks are gone and a replacement instance may have started."""

    def __init__(self) -> None:
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def require_open(self) -> None:
        if self._closed:
            raise ShuttingDownError


class _Releasable(Protocol):
    def release(self) -> None: ...


@dataclass(frozen=True)
class ShutdownReport:
    finished: int  # jobs in flight that reached a checkpoint in time
    abandoned: int  # jobs still running when the delay ran out
    flushed: int  # Huey locks freed
    slots_released: int


class GracefulShutdown:
    def __init__(
        self,
        pools: JobPools,
        *,
        instance_lock: _Releasable,
        release_resource_locks: Callable[[], object] | None = None,
        checkpoint_s: float = DEFAULT_CHECKPOINT_S,
        barrier: WriteBarrier | None = None,
    ) -> None:
        self._pools = pools
        self._lock = instance_lock
        self._release_resource_locks = release_resource_locks
        self._checkpoint_s = checkpoint_s
        self.barrier = barrier or WriteBarrier()
        self._report: ShutdownReport | None = None
        self._entered = False

    def run(self) -> ShutdownReport:
        """Idempotent from the first instruction: a second SIGTERM (even one that interrupts
        the first run in its own thread) changes nothing and returns at once."""
        if self._entered:
            return self._report or ShutdownReport(0, 0, 0, 0)
        self._entered = True
        log = structlog.get_logger()
        pools = self._pools
        pools.stop_claiming()  # 1
        in_flight = pools.running
        pools.wait_idle(self._checkpoint_s)  # 2
        abandoned = pools.running
        self.barrier.close()  # an abandoned job writes nothing from here on
        if abandoned:
            log.warning("shutdown.jobs_abandoned", count=abandoned)
        flushed = slots = 0
        try:  # 3: each release on its own; a read-only or locked database holds nothing of ours
            try:
                flushed = pools.flush_huey_locks()
            except (OperationalError, sqlite3.OperationalError) as error:
                log.warning("shutdown.huey_locks_not_flushed", error=type(error).__name__)
            try:
                slots = pools.slots.release_all()
            except (OperationalError, sqlite3.OperationalError) as error:
                log.warning("shutdown.slots_not_released", error=type(error).__name__)
            try:
                if self._release_resource_locks is not None:
                    self._release_resource_locks()
            except (OperationalError, sqlite3.OperationalError) as error:
                log.warning("shutdown.locks_not_released", error=type(error).__name__)
        finally:
            self._lock.release()
        self._report = ShutdownReport(in_flight - abandoned, abandoned, flushed, slots)
        return self._report
