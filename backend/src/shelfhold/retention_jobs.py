"""The retention jobs (ENF-OPS-009).

- `retention.expire` (daily): events, finished jobs and the audit past their age leave the
  database, and so do the versions of a series block that the retention no longer keeps (L49a).
  The database alone: no disk is woken, so it runs at any hour.
- `retention.early_purge` (every 10 minutes, EF-BIB-030): while a volume is under its free-space
  mark, the bins are purged early, oldest first (see `BinReclaimer.early_purge`). It is not parked
  by the disk guard (it removes, never adds) and it is not a window job: a full disk waits for no
  window.
- `retention.recycle` (daily, window job): the bins `.shelfhold/recycle/<id>` of the writable
  roots are looked at, and an entry older than 30 days is removed through `Fsops`, which holds
  the lease, the perimeter (the bin, nothing else) and the age (`trash_retention_purge`, read
  from the file system, never from here). The removal is announced first as an intention
  (`delete`), so a crash leaves a trace that the next start settles.
- `retention.caches` (daily, M6 L12, EF-BIB-018): the quotas and the retention of the derived
  caches (thumbnails, renditions and KEPUB, HTTP and metadata caches), LRU by last access, by
  the system emitter `cache_retention_purge` — host-created paths only. Only `/data` is touched
  (no library disk is woken), so it runs at any hour; a rendition an in-flight `/content` ticket
  is serving is pinned and stays.

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
from shelfhold.library.public import CachePolicy, CacheSweep, RenditionService, purge_caches
from shelfhold.ops.reclaim import backups_of
from shelfhold.reading.bundle import prune_versions

__all__ = [
    "CACHES_KIND",
    "EARLY_PURGE_KIND",
    "RECYCLE_KIND",
    "RETENTION_KIND",
    "ReclaimReport",
    "RecycleReport",
    "early_purge_trash",
    "register_retention_jobs",
    "sweep_caches",
    "sweep_recycle",
]

log = structlog.get_logger(__name__)

RETENTION_KIND: Final = "retention.expire"
RECYCLE_KIND: Final = "retention.recycle"
EARLY_PURGE_KIND: Final = "retention.early_purge"
CACHES_KIND: Final = "retention.caches"
EARLY_PURGE_CRON: Final = "*/10 * * * *"
RETENTION_CRON: Final = "35 4 * * *"
RECYCLE_CRON: Final = "50 3 * * *"  # inside the default window; clamped into any other
CACHES_CRON: Final = "05 5 * * *"  # after the history and the bins: /data only, any hour


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
    loaded = comp.load_roots()  # the database only: no root is opened, no lease taken
    if loaded is None:
        raise NoLibraryError("no physical root is registered")
    if not any(pressure(root.path) for root in loaded.expected):
        return ReclaimReport()  # nothing under its mark: the disks are left asleep (ENF-OPS-014)
    with comp.write_context() as w:
        roots = w.writable_roots
        journal = IntentJournal(comp.engine, w.fsops, clock=comp.clock)
        reclaimer = BinReclaimer(
            w.fsops, journal, roots, clock=comp.clock, backups=backups_of(comp)
        )
        return reclaimer.early_purge(pressure=lambda: any(pressure(root) for root in roots))


def sweep_caches(comp: Composition) -> CacheSweep:
    """One pass of the quotas and the retention over the derived caches (EF-BIB-018): what an
    in-flight request is serving is pinned and stays — the sweep asks the pin again under its
    lock at every file it unlinks, so a request that began since the pass started is honoured
    all the same. The low-disk guard of M3 passes before the quotas: while the volume of `/data`
    is under its free-space mark, the sweep evicts until it is back above."""
    gate = comp.gates.disk_pressure
    return purge_caches(
        comp.engine,
        comp.settings.data_dir,
        comp.clock,
        CachePolicy.from_settings(comp.settings),
        pinned=RenditionService.pinned(),
        pressure=None if gate is None else (lambda: gate(str(comp.settings.data_dir))),
    )


def register_retention_jobs(pools: JobPools, comp: Composition) -> None:
    policy = RetentionPolicy.from_settings(comp.settings)

    def expire(context: JobContext) -> None:
        now = comp.clock.now()
        history = expire_events_and_jobs(comp.engine, now=now, policy=policy)
        audit = expire_audit(comp.engine, now=now, policy=policy)
        bundles = prune_versions(comp.engine, now)  # M4 L49a: 3 versions, or used in 90 days
        context.record(events=history.events, jobs=history.jobs, audit=audit, bundles=bundles)

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

    def caches(context: JobContext) -> None:
        sweep = sweep_caches(comp)
        context.record(
            thumbnails_files=sweep.thumbnails_files,
            thumbnails_rows=sweep.thumbnails_rows,
            renditions_files=sweep.renditions_files,
            renditions_rows=sweep.renditions_rows,
            http_files=sweep.http_files,
            http_rows=sweep.http_rows,
            metadata_rows=sweep.metadata_rows,
        )

    pools.register(RETENTION_KIND, pool="maint", handler=expire, writes=False)
    pools.register(EARLY_PURGE_KIND, pool="maint", handler=early_purge, writes=False)
    pools.scheduler.add(EARLY_PURGE_KIND, EARLY_PURGE_CRON, jitter_s=30)
    pools.scheduler.add(RETENTION_KIND, RETENTION_CRON, jitter_s=300)
    pools.register(CACHES_KIND, pool="maint", handler=caches, writes=False)
    pools.scheduler.add(CACHES_KIND, CACHES_CRON, jitter_s=300)
    pools.register(RECYCLE_KIND, pool="maint", handler=recycle, windowed=True)
    pools.scheduler.add(RECYCLE_KIND, RECYCLE_CRON, jitter_s=120, windowed=True)
