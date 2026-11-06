"""What the composition root plugs together for the operating guards (L37).

- the two periodic jobs: `ops.disk_check` (every minute) and `target.access_audit` (once a day,
  EF-EXP-055: a Kavita account created after the link is found at the next cycle at the latest);
- the write guard of a Kavita client (`set_write_guard(target_write_guard(...))`, L26).
"""

import asyncio
from collections.abc import Callable, Iterable
from pathlib import Path

import structlog

from shelfhold.config import Settings
from shelfhold.export.targets.kavita import KavitaClient
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.infrastructure.observability.disk_guard import DiskGuard, DiskThresholds
from shelfhold.jobs.huey_app import JobContext, JobPools

OPS_JOBS = ("ops.disk_check", "target.access_audit")
DISK_CRON = "* * * * *"
AUDIT_CRON = "17 4 * * *"  # daily, off the hour and after the maintenance window opens


def thresholds_of(settings: Settings) -> DiskThresholds:
    return DiskThresholds(
        settings.disk_pause_percent,
        settings.disk_pause_gib,
        settings.disk_resume_percent,
        settings.disk_resume_gib,
    )


def disk_guard_for(settings: Settings, library_roots: Iterable[str] = ()) -> DiskGuard:
    """`/data`, `/store` and every physical root of the library (ENF-OPS-006)."""
    volumes = {"data": settings.data_dir, "store": settings.store_dir}
    for number, root in enumerate(library_roots, start=1):
        volumes["library" if number == 1 else f"library_{number}"] = Path(root)
    return DiskGuard(volumes, thresholds=thresholds_of(settings))


def register_ops_jobs(
    pools: JobPools,
    settings: Settings,
    *,
    disk_check: Callable[[], object],
    audit: Callable[[], object],
) -> None:
    """Registered before `boot_jobs`' scan, so the catch-up sees them."""

    def run_disk_check(_context: JobContext) -> None:
        disk_check()

    def run_audit(_context: JobContext) -> None:
        audit()

    pools.register("ops.disk_check", pool="io", handler=run_disk_check, writes=False)
    pools.register("target.access_audit", pool="io", handler=run_audit, writes=False)
    pools.scheduler.add("ops.disk_check", DISK_CRON)
    pools.scheduler.add("target.access_audit", AUDIT_CRON, jitter_s=600)


def daily_access_audit(
    auditor: AccessAuditor, clients: Callable[[], Iterable[tuple[str, KavitaClient]]]
) -> Callable[[], None]:
    """The body of `target.access_audit`: replay the read-only audit on every connected target.
    `AccessAuditor.audit` fails closed (a pause with `audit_failed`), so no error escapes."""

    def run() -> None:
        for target_id, client in clients():
            report = asyncio.run(auditor.audit(client, target_id))
            structlog.get_logger().info(
                "target.access_audit", target_id=target_id, failed=report.failed
            )

    return run
