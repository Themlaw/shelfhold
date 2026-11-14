"""The window jobs that keep /store (EF-BIB-027, EF-BIB-042 (4), EF-INT-027).

- `store.keep_catchup`: the Units of a shelf in "keep a copy" mode that are still in passthrough
  get their independent copy in /store, in batches under the budget of the volume (free space
  above the disk guard's reserve; 200 Units or 5 GiB per cycle). What does not fit is a backlog,
  said once (`unit.safeguard_backlog`) and taken up by the next night; the job stops between two
  Units when the window closes and resumes from the cursor (`JobContext.run_steps`).
- `store.index`: `store/INDEX.jsonl` rewritten when it would change, without a line for a content
  under a tombstone (EF-INT-027 (2)).

Both are window jobs (ENF-OPS-014): by day nothing wakes the disks of /store.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

import structlog

from shelfhold.composition import Composition, KeyringUnavailableError, NoLibraryError
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import LockBusyError
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.observability.disk_guard import disk_usage
from shelfhold.infrastructure.observability.wiring import thresholds_of
from shelfhold.jobs.public import JobContext, JobPools
from shelfhold.library.public import (
    KeepCopier,
    LinkPolicy,
    Safeguard,
    SafeguardFailedError,
    StoreBudget,
    StoreIndex,
    keep_candidates,
    plan_batch,
)
from shelfhold.work_locks import index_holder, work_holder

__all__ = ["KEEP_CATCHUP_KIND", "STORE_INDEX_KIND", "register_store_jobs", "store_budget"]

log = structlog.get_logger(__name__)

KEEP_CATCHUP_KIND: Final = "store.keep_catchup"
STORE_INDEX_KIND: Final = "store.index"
KEEP_CATCHUP_CRON: Final = "20 3 * * *"  # opens the window's work on /store
STORE_INDEX_CRON: Final = "40 5 * * *"  # after the catch-up of the night, still inside the window


def store_budget(comp: Composition) -> StoreBudget:
    """The room of the volume that holds /store: free space above the disk guard's pause mark."""
    usage = disk_usage(Path(comp.settings.store_dir))
    reserve = thresholds_of(comp.settings).pause_below(usage.total)
    return StoreBudget(free_bytes=usage.free, reserve_bytes=reserve)


def keep_catchup_handler(comp: Composition) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        candidates = keep_candidates(comp.engine)  # a read of the database: no disk woken
        if not candidates:
            return
        batch = plan_batch(candidates, store_budget(comp))
        context.record(queued=len(batch.selected), queued_bytes=batch.bytes, backlog=batch.backlog)
        if batch.backlog:  # said once per run; the unit ids stay in the database
            with write_transaction(comp.engine) as connection:
                comp.bus.publish(connection, "unit.safeguard_backlog", {"count": batch.backlog})
        if not batch.selected:
            return
        try:
            with comp.write_context() as w:
                store_dir = str(comp.settings.store_dir)
                safeguard = Safeguard(
                    comp.engine, w.fsops, w.loaded.locate, store_dir, comp.settings.data_dir
                )
                keeper = KeepCopier(
                    comp.engine, w.fsops, w.loaded.locate, store_dir, comp.settings.data_dir,
                    IntentJournal(comp.engine, w.fsops, clock=comp.clock),
                    extract=safeguard.run, hold_work=work_holder(comp),
                    links=LinkPolicy(comp.engine),  # EF-EXP-015: this keeper only
                )  # fmt: skip

                def step(unit_id: str) -> Callable[[], None]:
                    def keep() -> None:
                        try:
                            keeper.keep(unit_id)
                            context.add(kept=1)
                        except SafeguardFailedError as error:
                            log.warning("store.keep_failed", unit_id=unit_id, reason=error.reason)
                            context.add(failed=1)

                    return keep

                context.run_steps([step(c.unit_id) for c in batch.selected])
        except (NoLibraryError, KeyringUnavailableError) as error:
            log.info("store.keep_skipped", reason=type(error).__name__)
            context.record(skipped=type(error).__name__)

    return run


def store_index_handler(comp: Composition) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        try:
            with comp.write_context() as w:
                index = StoreIndex(
                    comp.engine, w.fsops, str(comp.settings.store_dir), w.tombstones,
                    hold=index_holder(comp),
                )  # fmt: skip
                report = index.rewrite()
        except (NoLibraryError, KeyringUnavailableError, LockBusyError) as error:
            context.record(skipped=type(error).__name__)  # a purge holds the index: next night
            return
        context.record(lines=report.lines, tombstoned=report.tombstoned, rewritten=report.rewritten)

    return run


def register_store_jobs(pools: JobPools, comp: Composition) -> None:
    pools.register(
        KEEP_CATCHUP_KIND, pool="maint", handler=keep_catchup_handler(comp), windowed=True
    )
    pools.scheduler.add(KEEP_CATCHUP_KIND, KEEP_CATCHUP_CRON, jitter_s=120, windowed=True)
    pools.register(STORE_INDEX_KIND, pool="maint", handler=store_index_handler(comp), windowed=True)
    pools.scheduler.add(STORE_INDEX_KIND, STORE_INDEX_CRON, jitter_s=120, windowed=True)
