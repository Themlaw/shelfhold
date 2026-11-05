"""Graceful shutdown on SIGTERM (ENF-OPS-007), in three steps.

1. stop taking tasks (no job is claimed, the periodic scan queues nothing);
2. let the jobs in flight reach a checkpoint, for at most `checkpoint_s`; one that does not is
   abandoned. Its file-operation intent stays open on purpose: the next start settles it
   (replay or cancel) before any job runs (EF-EXP-041), and its `running` row is requeued;
3. free the locks: Huey's, the domain slots, the resource locks, then the instance lock.

Nothing is deleted and no intent is closed here. The instance lock goes last, so a replacement
instance never starts while this one can still write.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import structlog
from sqlalchemy.exc import OperationalError

from shelfhold.jobs.huey_app import JobPools

DEFAULT_CHECKPOINT_S = 25.0  # under the 30 s a container runtime grants after SIGTERM


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
    ) -> None:
        self._pools = pools
        self._lock = instance_lock
        self._release_resource_locks = release_resource_locks
        self._checkpoint_s = checkpoint_s
        self._report: ShutdownReport | None = None

    def run(self) -> ShutdownReport:
        if self._report is not None:
            return self._report  # idempotent: a second signal changes nothing
        log = structlog.get_logger()
        pools = self._pools
        pools.stop_claiming()  # 1
        in_flight = pools.running
        pools.wait_idle(self._checkpoint_s)  # 2
        abandoned = pools.running
        if abandoned:
            log.warning("shutdown.jobs_abandoned", count=abandoned)
        flushed = slots = 0
        try:  # 3 (a read-only or locked database cannot be written: nothing was held in it)
            flushed = pools.flush_huey_locks()
            slots = pools.slots.release_all()
            if self._release_resource_locks is not None:
                self._release_resource_locks()
        except OperationalError as error:
            log.warning("shutdown.locks_not_released", error=type(error).__name__)
        finally:
            self._lock.release()
        self._report = ShutdownReport(in_flight - abandoned, abandoned, flushed, slots)
        return self._report
