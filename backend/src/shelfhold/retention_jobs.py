"""The retention jobs (ENF-OPS-009).

- `retention.expire` (daily): events, finished jobs and the audit past their age leave the
  database. The database alone: no disk is woken, so it runs at any hour.
- `retention.early_purge` (every 10 minutes, EF-BIB-030): while a volume is under its free-space
  mark, the bins are purged early, oldest first (see `BinReclaimer.early_purge`). It is not parked
  by the disk guard (it removes, never adds) and it is not a window job: a full disk waits for no
  window.
- `retention.recycle` (daily, window job): the bins `.shelfhold/recycle/<id>` of the writable
  roots are looked at, and an entry older than 30 days is removed through `Fsops`, which holds
  the lease, the perimeter (the bin, nothing else) and the age (`trash_retention_purge`, read
  from the file system, never from here). The removal is announced first as an intention
  (`delete`), so a crash leaves a trace that the next start settles.

Both stand outside the freeze: the scheduler does not run a job while the instance is frozen.
"""

from __future__ import annotations

from typing import Final

import structlog

from shelfhold.composition import Composition, KeyringUnavailableError, NoLibraryError
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.reclaim import BinReclaimer, ReclaimReport
from shelfhold.export.application.recycle_sweep import (
    RecycleReport,
    RecycleSweeper,
)
from shelfhold.jobs.public import JobContext, JobPools
from shelfhold.jobs.retention import RetentionPolicy, expire_audit, expire_events_and_jobs
from shelfhold.ops.reclaim import backups_of

__all__ = [
    "EARLY_PURGE_KIND",
    "RECYCLE_KIND",
    "RETENTION_KIND",
    "ReclaimReport",
    "RecycleReport",
    "early_purge_trash",
    "register_retention_jobs",
    "sweep_recycle",
]

log = structlog.get_logger(__name__)

RETENTION_KIND: Final = "retention.expire"
RECYCLE_KIND: Final = "retention.recycle"
EARLY_PURGE_KIND: Final = "retention.early_purge"
EARLY_PURGE_CRON: Final = "*/10 * * * *"
RETENTION_CRON: Final = "35 4 * * *"
RECYCLE_CRON: Final = "50 3 * * *"  # inside the default window; clamped into any other


def sweep_recycle(comp: Composition) -> RecycleReport:
    """Remove the bin entries that are 30 days old, under an intention each. A copy of /store that
    a retained integrated backup may still look for stays (EF-BIB-021)."""
    backups = backups_of(comp)
    with comp.write_context() as w:
        journal = IntentJournal(comp.engine, w.fsops, clock=comp.clock)
        return RecycleSweeper(w.fsops, journal, w.writable_roots, backups=backups).sweep()


def early_purge_trash(comp: Composition) -> ReclaimReport:
    """The early purge of the bins while a volume is under its mark (EF-BIB-030). Nothing happens
    without a disk guard (a command-line process has none): fsops refuses the emitter then."""
    pressure = comp.gates.disk_pressure
    if pressure is None:
        return ReclaimReport()
    with comp.write_context() as w:
        roots = w.writable_roots
        journal = IntentJournal(comp.engine, w.fsops, clock=comp.clock)
        reclaimer = BinReclaimer(
            w.fsops, journal, roots, clock=comp.clock, backups=backups_of(comp)
        )
        return reclaimer.early_purge(pressure=lambda: any(pressure(root) for root in roots))


def register_retention_jobs(pools: JobPools, comp: Composition) -> None:
    policy = RetentionPolicy.from_settings(comp.settings)

    def expire(context: JobContext) -> None:
        now = comp.clock.now()
        history = expire_events_and_jobs(comp.engine, now=now, policy=policy)
        audit = expire_audit(comp.engine, now=now, policy=policy)
        context.record(events=history.events, jobs=history.jobs, audit=audit)

    def recycle(context: JobContext) -> None:
        try:
            report = sweep_recycle(comp)
        except (NoLibraryError, KeyringUnavailableError) as error:
            context.record(skipped=type(error).__name__)
            return
        context.record(
            removed=report.removed,
            kept=report.kept,
            failed=report.failed,
            held_irreplaceable=report.held_irreplaceable,
        )

    def early_purge(context: JobContext) -> None:
        try:
            report = early_purge_trash(comp)
        except (NoLibraryError, KeyringUnavailableError) as error:
            context.record(skipped=type(error).__name__)
            return
        context.record(
            removed=report.removed,
            kept=report.kept,
            held_irreplaceable=report.held_irreplaceable,
            held_other=report.held_other,
            failed=report.failed,
        )

    pools.register(RETENTION_KIND, pool="maint", handler=expire, writes=False)
    pools.register(EARLY_PURGE_KIND, pool="maint", handler=early_purge, writes=False)
    pools.scheduler.add(EARLY_PURGE_KIND, EARLY_PURGE_CRON, jitter_s=30)
    pools.scheduler.add(RETENTION_KIND, RETENTION_CRON, jitter_s=300)
    pools.register(RECYCLE_KIND, pool="maint", handler=recycle, windowed=True)
    pools.scheduler.add(RECYCLE_KIND, RECYCLE_CRON, jitter_s=120, windowed=True)
