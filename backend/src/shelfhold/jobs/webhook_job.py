"""The job `webhook.deliver`: the sweep of the webhook outbox (EF-INT-005).

Every minute it makes the rows of the events the subscriptions have not seen and sends what is due
(a retry waits its own delay, which doubles). Nothing here chooses an address: the Fetcher does,
behind its guard, at each send. One active job at a time (a constant dedupe key: no row, no key and
no address is in it).
"""

from __future__ import annotations

from typing import Final, Protocol

from shelfhold.jobs.public import JobContext, JobPools

KIND: Final = "webhook.deliver"
SWEEP_CRON: Final = "* * * * *"


class Sweep(Protocol):
    def run_once(self) -> object: ...


def register_webhook_job(pools: JobPools, sweep: Sweep) -> None:
    def handle(context: JobContext) -> None:
        sweep.run_once()

    pools.register(KIND, pool="io", handler=handle)
    pools.scheduler.add(KIND, SWEEP_CRON)
