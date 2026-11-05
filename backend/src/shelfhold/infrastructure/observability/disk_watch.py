"""Ties the disk guard to the job queues and the event bus (ENF-OPS-006, ENF-OPS-011).

One `check()` per minute (the `ops.disk_check` job): a volume that goes under its mark announces
`disk.low` once and parks the write queues; when every volume is back over the resume mark the
queues are sent again, with an `ops.disk_recovered` for the record (no notification).
"""

from sqlalchemy import Engine

from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.infrastructure.observability.disk_guard import DISK_LOW, DiskGuard
from shelfhold.jobs.huey_app import JobPools


class DiskWatcher:
    def __init__(self, guard: DiskGuard, pools: JobPools, *, engine: Engine, bus: EventBus) -> None:
        self._guard = guard
        self._pools = pools
        self._engine = engine
        self._bus = bus
        self._synced = False  # the first measure also frees what a previous run parked

    def check(self) -> None:
        state = self._guard.evaluate()
        published: list[PublishedEvent] = []
        if state.changes:
            with self._engine.begin() as connection:
                for change in state.changes:
                    published.append(
                        self._bus.publish(
                            connection,
                            DISK_LOW if change.low else "ops.disk_recovered",
                            {"volume": change.volume, "free_percent": change.free_percent},
                        )
                    )
        if state.paused and self._pools.paused_reason != DISK_LOW:
            self._pools.pause_writes(DISK_LOW)
        elif not state.paused and (self._pools.paused_reason == DISK_LOW or not self._synced):
            # not synced yet: jobs parked by a run that ended low are sent again at boot
            self._pools.resume_writes(DISK_LOW)
        self._synced = True
        self._bus.deliver(published)
