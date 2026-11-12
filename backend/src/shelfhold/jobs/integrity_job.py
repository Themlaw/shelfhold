"""The window job `integrity_rolling` (EF-BIB-012, ENF-OPS-014).

Each night, inside the maintenance window and nowhere else, a bounded batch of the Units checked
the longest ago (or never) is read through the archive guard at a capped rate; a Unit found
corrupt is asked for again in full, and a fresh copy already received is verified and swapped in
by the same-path upgrade. The job stops between two Units when the window closes; a Unit checked
is not due again for a month, so the disks sleep the rest of the time. The rules are in
`library.application.integrity` and `acquisition.application.reacquire`; this module wires them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Final

import structlog

from shelfhold.acquisition.application.reacquire import Reacquirer
from shelfhold.composition import Composition, KeyringUnavailableError, NoLibraryError
from shelfhold.export.application.upgrade import SamePathUpgrade
from shelfhold.jobs.huey_app import JobContext, JobPools
from shelfhold.jobs.window import (
    MaintenanceWindow,
    OutsideMaintenanceWindowError,
    StoreOrigin,
    ensure_store_access,
)
from shelfhold.library.application.integrity import DEFAULT_RATE, IntegrityChecker, Throttle
from shelfhold.library.public import irreplaceability

__all__ = ["INTEGRITY_KIND", "integrity_handler", "register_integrity_job", "window_guard"]

log = structlog.get_logger(__name__)

INTEGRITY_KIND: Final = "integrity_rolling"  # the name `WINDOW_JOB_ORDER` ranks
INTEGRITY_CRON: Final = "50 3 * * *"  # inside the window, after the catch-up and the safeguard
BATCH: Final = 100  # Units read per night at most


def window_guard(now: Callable[[], datetime], window: MaintenanceWindow) -> Callable[[], None]:
    """Refuses (`OutsideMaintenanceWindowError`) before a periodic read of /store or /library
    outside the window: called before any file is opened."""

    def guard() -> None:
        ensure_store_access(StoreOrigin.PERIODIC, now=now(), window=window)

    return guard


def integrity_handler(comp: Composition, window: MaintenanceWindow) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        checker = IntegrityChecker(
            comp.engine, comp.bus, comp.clock,
            throttle=Throttle(DEFAULT_RATE), guard=window_guard(comp.clock.now, window),
        )  # fmt: skip
        reacquirer = Reacquirer(
            comp.engine, comp.bus, comp.clock,
            irreplaceable=lambda connection, unit: irreplaceability(connection, unit).irreplaceable,
        )  # fmt: skip

        def closing() -> bool:
            over = context.window_end is not None and comp.clock.now() >= context.window_end
            return over or context.stop_requested

        def swap(unit_id: str, content_id: str) -> list[str]:
            with comp.write_context() as w:
                upgrade = SamePathUpgrade(
                    comp.engine, w.fsops, locate=w.loaded.locate,
                    data_dir=comp.settings.data_dir, clock=comp.clock, bus=w.bus,
                )  # fmt: skip
                results = upgrade.upgrade_unit(unit_id, content_id, reason="integrity_reacquire")
                return [r.outcome.value for r in results]

        try:
            corrupt = replaced = requested = 0
            for unit_id in checker.due_units(BATCH):
                if closing():
                    context.preempted = True
                    break
                corrupt += checker.check_unit(unit_id).state == "corrupt"
            for unit_id in reacquirer.corrupt_units(BATCH):
                if closing():
                    context.preempted = True
                    break
                requested += reacquirer.reacquire(unit_id).value == "requested"
                done = reacquirer.complete(unit_id, verify=checker.verify_content, upgrade=swap)
                replaced += done.outcome in ("replaced", "fresh_copy_ready")
            context.record(corrupt=corrupt, requested=requested, replaced=replaced)
        except OutsideMaintenanceWindowError:
            context.preempted = True  # the window closed under the batch: tomorrow night
        except (NoLibraryError, KeyringUnavailableError) as error:
            log.info("integrity.skipped", reason=type(error).__name__)
            context.record(skipped=type(error).__name__)

    return run


def register_integrity_job(pools: JobPools, comp: Composition) -> None:
    pools.register(
        INTEGRITY_KIND, pool="maint", handler=integrity_handler(comp, pools.window), windowed=True
    )
    pools.scheduler.add(INTEGRITY_KIND, INTEGRITY_CRON, jitter_s=120, windowed=True)
