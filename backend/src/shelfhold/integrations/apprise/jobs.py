"""The job `notify.dispatch`: sweeps `notification_pending` (EF-INT-006).

One active job at a time (a constant dedupe key: the key, like the rows, carries no title). It is
queued after the commit of a publisher and by a periodic every five minutes, so an alert whose
queue entry was lost in a crash is sent at the next sweep.
"""

from __future__ import annotations

from typing import Final

from shelfhold.integrations.apprise.dispatch import NotificationDispatcher
from shelfhold.jobs.public import JobContext, JobPools

KIND: Final = "notify.dispatch"
DEDUPE_KEY: Final = "notify:dispatch"
SWEEP_CRON: Final = "*/5 * * * *"


def register_notification_job(pools: JobPools, dispatcher: NotificationDispatcher) -> None:
    def handle(context: JobContext) -> None:
        dispatcher.run_once()

    pools.register(KIND, pool="io", handler=handle)
    pools.scheduler.add(KIND, SWEEP_CRON)


def request_dispatch(pools: JobPools) -> None:
    """Call after the publisher's transaction committed."""
    pools.submit(KIND, dedupe_key=DEDUPE_KEY)
