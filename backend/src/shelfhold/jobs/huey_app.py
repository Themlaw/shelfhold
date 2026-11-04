"""Huey, behind its adapter (ENF-GOV-002): one instance per pool, one periodic, no business logic.

Pools (02 section 3.5): `io` (network, several threads), `cpu` (one thread), `maint` (one thread,
the maintenance window). Each has its own SQLite file in `/data` (created by `ensure_volumes`), so
a long job in one pool never holds the lock another pool's queue needs (spike S1). The only
periodic Huey knows is `tick`, every minute, which calls `Scheduler.scan`; everything periodic is
a row of `schedule`.

Priorities: a Huey task carries the numeric priority of its class, `new` > `request` > `backfill`,
so a backlog (catch-up in batches) never delays a new chapter.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from huey import SqliteHuey, crontab
from huey.api import Huey
from huey.exceptions import RetryTask
from sqlalchemy import Engine

from shelfhold.infrastructure.clock import Clock
from shelfhold.jobs.ledger import Created, JobLedger, PendingJob
from shelfhold.jobs.scheduler import ScanReport, Scheduler
from shelfhold.jobs.slots import RETRY_DELAY_S, DomainSlots
from shelfhold.jobs.window import DEFAULT_WINDOW, MaintenanceWindow

POOLS = ("io", "cpu", "maint")
# Worker threads each pool's consumer is started with (`huey_consumer -w N -k thread`).
POOL_WORKERS = {"io": 4, "cpu": 1, "maint": 1}
CLASS_PRIORITY = {"new": 30, "request": 20, "backfill": 10}
TICK_POOL = "io"
TICK_PRIORITY = 100  # above every class: queued downloads never delay the scan (a task already
# running is not preempted, so `io` keeps a spare worker thread)
_LOCK_MARK = ".lock."


class InstanceLockNotHeldError(RuntimeError):
    """`flush_locks` before the instance lock is ours would free a live instance's locks."""


class InstanceLock(Protocol):
    def is_held(self) -> bool: ...


@dataclass(frozen=True)
class JobContext:
    job_id: str
    kind: str
    dedupe_key: str
    scope_key: str
    limit_key: str | None
    window_end: datetime | None  # the end of the open window, for a job that has to stop by then


Handler = Callable[[JobContext], None]


@dataclass(frozen=True)
class BootReport:
    flushed: int
    slots_released: int
    requeued: int
    queued: int


class JobPools:
    def __init__(
        self,
        data_dir: Path,
        *,
        engine: Engine,
        clock: Clock,
        seed: str,
        window: MaintenanceWindow = DEFAULT_WINDOW,
    ) -> None:
        self.clock = clock
        self.window = window
        self.ledger = JobLedger(engine, clock)
        self.slots = DomainSlots(engine, clock)
        self.scheduler = Scheduler(engine, clock=clock, seed=seed, dispatcher=self, window=window)
        self._engine = engine
        self._handlers: dict[str, tuple[str, Handler, bool]] = {}
        self._hueys: dict[str, Huey] = {}
        self._run_job: dict[str, Any] = {}
        for name in POOLS:
            huey = SqliteHuey(
                name=f"shelfhold-{name}",
                filename=str(data_dir / f"huey-{name}.db"),
                utc=True,
                results=False,
            )
            self._hueys[name] = huey
            self._run_job[name] = huey.task(name="run_job")(self._execute)
        self._hueys[TICK_POOL].periodic_task(
            crontab(minute="*"), name="tick", priority=TICK_PRIORITY
        )(self.tick)

    # -- Huey instances ---------------------------------------------------------------------

    @property
    def names(self) -> tuple[str, ...]:
        return POOLS

    def huey(self, pool: str) -> Huey:
        return self._hueys[pool]

    def periodic_tasks(self) -> list[tuple[str, str]]:
        return [
            (name, task.name)
            for name, huey in self._hueys.items()
            for task in huey._registry.periodic_tasks
        ]

    def tick_matches(self, instant: datetime) -> bool:
        return all(
            task.validate_datetime(instant)
            for huey in self._hueys.values()
            for task in huey._registry.periodic_tasks
        )

    # -- registration and submission --------------------------------------------------------

    def register(self, kind: str, *, pool: str, handler: Handler, windowed: bool = False) -> None:
        """`windowed`: the job runs only inside the maintenance window and stops at its end."""
        if pool not in self._hueys:
            raise KeyError(pool)
        self._handlers[kind] = (pool, handler, windowed)

    def submit(
        self,
        kind: str,
        *,
        dedupe_key: str,
        limit_key: str | None = None,
        source_key: str | None = None,
        priority_class: str = "new",
    ) -> Created:
        """Queue a job once per dedupe key (for a download, the chapter)."""
        with self._engine.begin() as conn:
            created = self.ledger.create(
                conn,
                kind=kind,
                dedupe_key=dedupe_key,
                source_key=source_key,
                limit_key=limit_key,
                priority_class=priority_class,
            )
        if created.created:
            self.ledger.mark_dispatched(
                created.job_id, self.dispatch(self.ledger.get(created.job_id))
            )
        return created

    def tick(self) -> ScanReport:
        """The one periodic: scan `schedule` and queue what is due."""
        return self.scheduler.scan()

    # -- Dispatcher -------------------------------------------------------------------------

    def dispatch(self, job: PendingJob) -> str:
        route = self._handlers.get(job.kind)
        if route is None:  # a schedule row from another version must not stop every scan
            self.ledger.mark_finished(job.job_id, error="no handler", error_code="job.unknown_kind")
            return f"unrouted:{job.job_id}"
        pool = route[0]
        priority = CLASS_PRIORITY.get(job.priority_class, CLASS_PRIORITY["new"])
        task = self._run_job[pool].s(job.job_id, priority=priority)
        self._hueys[pool].enqueue(task)
        return str(task.id)

    # -- the task body, run by a worker thread ----------------------------------------------

    def _execute(self, job_id: str) -> None:
        job = self.ledger.get(job_id)
        route = self._handlers.get(job.kind)
        if route is None:
            self.ledger.mark_finished(job_id, error="no handler", error_code="job.unknown_kind")
            return
        window_end = None
        if route[2]:  # a window job: its window is the one it was queued in, never today's
            now = self.clock.now()
            span = self.window.span_containing(job.created_at) if job.created_at else None
            if span is None or now >= span.end:
                following = self.window.next_span(now)
                if self.ledger.reschedule_to(job_id, following.start):
                    raise RetryTask(eta=following.start.replace(tzinfo=None))
                return  # not queued any more: a duplicate of a job that already ran
            if now < span.start:
                raise RetryTask(eta=span.start.replace(tzinfo=None))
            window_end = span.end
        if not self.ledger.claim_running(job_id):
            return  # the same job reached us twice: the other task has it, or it is done
        if job.limit_key is not None and not self.slots.try_acquire(job.limit_key):
            self.ledger.mark_deferred(job_id)
            raise RetryTask(delay=RETRY_DELAY_S)  # asked again later, no thread kept waiting
        try:
            route[1](
                JobContext(
                    job_id, job.kind, job.dedupe_key, job.scope_key, job.limit_key, window_end
                )
            )
        except Exception as exc:
            # the class name only: an upstream message may carry a title (EF-BIB-053)
            self.ledger.mark_finished(job_id, error=type(exc).__name__, error_code="job.failed")
        else:
            self.ledger.mark_finished(job_id)
        finally:
            if job.limit_key is not None:
                self.slots.release(job.limit_key)


def boot_jobs(pools: JobPools, *, instance_lock: InstanceLock) -> BootReport:
    """Start-up, in this order, and only once the instance lock is ours (ENF-OPS-007, -015).

    1. `flush_locks` on every pool, 2. release the domain leases, 3. put the jobs that were
    `running` back (a killed worker is not acknowledged), 4. one scan: the catch-up of whatever
    came due during the downtime, once.
    """
    if not instance_lock.is_held():
        raise InstanceLockNotHeldError("flush_locks needs the instance lock")
    flushed = 0
    for name in pools.names:
        huey = pools.huey(name)
        prefix = f"{huey.name}{_LOCK_MARK}"
        names = [k[len(prefix) :] for k in huey.storage.result_items() if k.startswith(prefix)]
        flushed += len(huey.flush_locks(*names))
    released = pools.slots.release_all()
    requeued = pools.ledger.requeue_running()
    report = pools.scheduler.startup_scan()  # also sends the requeued rows
    return BootReport(flushed, released, len(requeued), len(report.queued))
