"""The job `notify.digest`: the sweep of per-person notifications (EF-INT-021).

Every five minutes it notices the outages that have lasted an hour, keeps the daily reminder of a
`withdrawal_pending` that is still open, and sends what is due: a digest at 08:00 local time of
each profile, an immediate notice at once. Nothing is applied here. One active job at a time (a
constant dedupe key: neither the key nor the rows carry a title).
"""

from __future__ import annotations

from typing import Final, Protocol

from shelfhold.jobs.public import JobContext, JobPools

KIND: Final = "notify.digest"
SWEEP_CRON: Final = "*/5 * * * *"


class Sweep(Protocol):
    def run_once(self) -> object: ...


def register_digest_job(pools: JobPools, sweep: Sweep) -> None:
    def handle(context: JobContext) -> None:
        sweep.run_once()

    pools.register(KIND, pool="io", handler=handle)
    pools.scheduler.add(KIND, SWEEP_CRON)
