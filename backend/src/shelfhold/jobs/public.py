"""What other contexts may use of the job system."""

from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.jobs.errors import JobFailedError
from shelfhold.jobs.huey_app import BootReport, JobContext, JobPools, boot_jobs
from shelfhold.jobs.ledger import MAX_ATTEMPTS
from shelfhold.jobs.slots import DomainSlots
from shelfhold.jobs.window import (
    DEFAULT_WINDOW,
    OutsideMaintenanceWindowError,
    StoreOrigin,
    ensure_store_access,
    run_until,
)

__all__ = [
    "DEFAULT_WINDOW",
    "MAX_ATTEMPTS",
    "BootReport",
    "Clock",
    "DomainSlots",
    "JobContext",
    "JobFailedError",
    "JobPools",
    "OutsideMaintenanceWindowError",
    "StoreOrigin",
    "SystemClock",
    "boot_jobs",
    "ensure_store_access",
    "run_until",
]
