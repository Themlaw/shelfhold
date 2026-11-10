"""`backup.run`: the daily backup (EF-INT-015), outside the maintenance window.

A backup touches `/data` and the backup directory, never `/store` or a library root (a scheduled
run leaves the provenance mirrors out, see `BackupService.run`), so no disk is woken and the job
is not a window job. It runs at 01:40, before the window opens (03:00 by default), and holds the
maintenance gate for the copy, so that a `VACUUM` already running makes it wait and one that
comes later is refused. A run that fails is a failed job under the code `backup.failed`; the
event and its cause were published by the service.
"""

from __future__ import annotations

from typing import Final

from shelfhold.jobs.errors import JobFailedError
from shelfhold.jobs.public import JobContext, JobPools
from shelfhold.ops.backup.run import BackupError, BackupService

__all__ = ["BACKUP_CRON", "BACKUP_KIND", "register_backup_job"]

BACKUP_KIND: Final = "backup.run"
BACKUP_CRON: Final = "40 1 * * *"


def register_backup_job(pools: JobPools, service: BackupService) -> None:
    def run(context: JobContext) -> None:
        if not service.configured():  # a fresh install: nothing asked for, nothing to fail
            context.record(skipped="not_configured")
            return
        try:
            report = service.run()
        except BackupError as error:
            context.record(cause=error.reason)
            raise JobFailedError("backup.failed") from None
        context.record(result="ok", size=report.size, members=report.members,
                       pruned=len(report.pruned))  # fmt: skip

    pools.register(BACKUP_KIND, pool="maint", handler=run, writes=False)
    pools.scheduler.add(BACKUP_KIND, BACKUP_CRON, jitter_s=300)
