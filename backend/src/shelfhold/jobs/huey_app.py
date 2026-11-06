"""Huey, behind its adapter (ENF-GOV-002): one instance per pool, one periodic, no business logic.

Pools (02 section 3.5): `io` (network, several threads), `cpu` (one thread), `maint` (one thread,
the maintenance window). Each has its own SQLite file in `/data` (created by `ensure_volumes`), so
a long job in one pool never holds the lock another pool's queue needs (spike S1). The only
periodic Huey knows is `tick`, every minute, which calls `Scheduler.scan`; everything periodic is
a row of `schedule`.

Priorities: a Huey task carries the numeric priority of its class, `new` > `request` > `backfill`,
so a backlog (catch-up in batches) never delays a new chapter.
"""

import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from huey import SqliteHuey, crontab
from huey.api import Huey
from huey.consumer import Consumer
from huey.exceptions import RetryTask
from sqlalchemy import Engine

from shelfhold.infrastructure.clock import Clock
from shelfhold.jobs.errors import JobFailedError
from shelfhold.jobs.ledger import Created, JobLedger, PendingJob
from shelfhold.jobs.scheduler import ScanReport, Scheduler
from shelfhold.jobs.slots import RETRY_DELAY_S, DomainSlots
from shelfhold.jobs.window import DEFAULT_WINDOW, MaintenanceWindow, WindowRun, run_until

POOLS = ("io", "cpu", "maint")
# Worker threads each pool's consumer is started with (`huey_consumer -w N -k thread`).
POOL_WORKERS = {"io": 4, "cpu": 1, "maint": 1}
CLASS_PRIORITY = {"new": 30, "request": 20, "backfill": 10}
TICK_POOL = "io"
TICK_PRIORITY = 100  # above every class: queued downloads never delay the scan (a task already
# running is not preempted, so `io` keeps a spare worker thread)
_LOCK_MARK = ".lock."


class _EmbeddedConsumer(Consumer):  # type: ignore[misc]
    """A Huey consumer inside the server process: the server owns the signals, not Huey."""

    def _set_signal_handlers(self) -> None:
        return None


class InstanceLockNotHeldError(RuntimeError):
    """`flush_locks` before the instance lock is ours would free a live instance's locks."""


class InstanceLock(Protocol):
    def is_held(self) -> bool: ...


@dataclass
class JobContext:
    job_id: str
    kind: str
    dedupe_key: str
    scope_key: str
    limit_key: str | None
    window_end: datetime | None  # a window job has to stop by then
    cursor: int  # the step a preempted window job resumes from (0 on a first run)
    clock: Clock
    ledger: JobLedger
    preempted: bool = False
    stop: Callable[[], bool] = lambda: False  # SIGTERM was received: checkpoint and leave

    @property
    def stop_requested(self) -> bool:
        return self.stop()

    def run_steps(self, steps: Sequence[Callable[[], None]]) -> WindowRun:
        """Run `steps[cursor:]` until the window closes, between two steps (ENF-OPS-014).

        The cursor is persisted on `job_view`: stopped at the end of the window, the job is parked
        with `next_index` and tonight's or tomorrow's run starts there, not at the beginning.
        """
        deadline = self.window_end or datetime.max.replace(tzinfo=UTC)
        report = run_until(
            steps, deadline=deadline, clock=self.clock, start=self.cursor, stop=self.stop
        )
        self.ledger.save_cursor(self.job_id, report.next_index if report.preempted else None)
        self.preempted = report.preempted
        return report


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
        self._handlers: dict[str, tuple[str, Handler, bool, bool]] = {}
        self._pause_lock = threading.RLock()  # pause, resume and the park of one job agree
        self._paused_reason: str | None = None  # a queue-wide pause of the write jobs (disk guard)
        self._stopping = False  # SIGTERM: no task is claimed any more (ENF-OPS-007)
        self._running = 0
        self._idle = threading.Condition()
        self._hueys: dict[str, Huey] = {}
        self._consumers: list[Consumer] = []
        self._run_job: dict[str, Any] = {}
        for name in POOLS:
            queue_file = data_dir / f"huey-{name}.db"
            # 0600 from the first byte: created here, before Huey opens them (it only opens), and
            # the SQLite side files too, which do not always inherit the mode of the main file
            for suffix in ("", "-wal", "-shm"):
                os.close(os.open(f"{queue_file}{suffix}", os.O_CREAT | os.O_WRONLY, 0o600))
            huey = SqliteHuey(
                name=f"shelfhold-{name}", filename=str(queue_file), utc=True, results=False
            )
            self._hueys[name] = huey
            self._run_job[name] = huey.task(name="run_job")(self._execute)
        self._hueys[TICK_POOL].periodic_task(
            crontab(minute="*"), name="tick", priority=TICK_PRIORITY
        )(self.tick)

    # -- the workers ------------------------------------------------------------------------

    def start_workers(self) -> None:
        """One consumer per pool, with its worker threads (spike S1). Without them the tasks that
        `boot_jobs` and the `tick` send would wait in the queue files for ever."""
        if self._consumers:
            return
        for name in POOLS:
            consumer = _EmbeddedConsumer(
                self._hueys[name], workers=POOL_WORKERS[name], worker_type="thread",
                check_worker_health=False, shutdown_timeout=10,
            )  # fmt: skip
            consumer.start()
            self._consumers.append(consumer)

    def stop_workers(self) -> None:
        """After `GracefulShutdown`: nothing is claimed any more, the threads leave."""
        consumers, self._consumers = self._consumers, []
        for consumer in consumers:
            consumer.stop(graceful=True)

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

    def register(
        self,
        kind: str,
        *,
        pool: str,
        handler: Handler,
        windowed: bool = False,
        writes: bool = True,
    ) -> None:
        """`windowed`: the job runs only inside the maintenance window and stops at its end.
        `writes`: the job writes to a volume, so a disk-guard pause parks it (reads go on)."""
        if pool not in self._hueys:
            raise KeyError(pool)
        self._handlers[kind] = (pool, handler, windowed, writes)

    # -- queue-wide pause (ENF-OPS-006) -------------------------------------------------------

    @property
    def paused_reason(self) -> str | None:
        return self._paused_reason

    def pause_writes(self, code: str) -> int:
        """Park every write job (queued now, or reaching a worker later) under `code`: no attempt
        is spent and no retry is scheduled. Nothing is deleted; reads and the UI are untouched."""
        with self._pause_lock:
            self._paused_reason = code
            kinds = [kind for kind, route in self._handlers.items() if route[3]]
            return self.ledger.pause_queued(code, kinds)

    def resume_writes(self, code: str) -> int:
        with self._pause_lock:
            if self._paused_reason == code:
                self._paused_reason = None
            return self._send_again(self.ledger.resume_paused(code))

    def _send_again(self, jobs: list[PendingJob]) -> int:
        for job in jobs:
            self.ledger.mark_dispatched(job.job_id, self.dispatch(job))
        return len(jobs)

    def _park_if_paused(self, job_id: str) -> bool:
        """True when the job was parked. Compare-and-set: if the pause ended while the row was
        being written, the job is taken back at once instead of staying parked for ever."""
        with self._pause_lock:
            code = self._paused_reason
            if code is None:
                return False
            self.ledger.pause_one(job_id, code)
            if self._paused_reason != code:  # resumed in between
                self._send_again(self.ledger.resume_paused(code, job_id))
            return True

    def flush_huey_locks(self) -> int:
        """`flush_locks` on every pool: the locks a worker of this run still held (boot and
        shutdown both)."""
        flushed = 0
        for name in self.names:
            huey = self.huey(name)
            prefix = f"{huey.name}{_LOCK_MARK}"
            names = [k[len(prefix) :] for k in huey.storage.result_items() if k.startswith(prefix)]
            flushed += len(huey.flush_locks(*names))
        return flushed

    # -- shutdown (ENF-OPS-007) ---------------------------------------------------------------

    @property
    def stopping(self) -> bool:
        return self._stopping

    def stop_claiming(self) -> None:
        """Step 1 of the shutdown: a task that reaches a worker now is left in the queue."""
        self._stopping = True

    @property
    def running(self) -> int:
        return self._running

    def wait_idle(self, timeout: float) -> bool:
        """Step 2: True when every job in flight has finished within `timeout` seconds."""
        with self._idle:
            return self._idle.wait_for(lambda: self._running == 0, timeout)

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
        if self._stopping:
            return ScanReport(())
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
        if self.scheduler.held:  # `restored_behind`: nothing runs until the adoption (EF-INT-026)
            raise RetryTask(delay=RETRY_DELAY_S)
        if self._stopping:
            self.ledger.forget_dispatch(job_id)  # the task is consumed: the next boot re-sends it
            return
        job = self.ledger.get(job_id)
        route = self._handlers.get(job.kind)
        if route is None:
            self.ledger.mark_finished(job_id, error="no handler", error_code="job.unknown_kind")
            return
        if route[3] and self._park_if_paused(job_id):
            return  # parked, not failed, not retried
        window_end = None
        if route[2]:  # a window job runs in the window it was queued for, as persisted
            now = self.clock.now()
            end = job.window_end
            if end is None or now >= end:  # queued by hand, or its night is over: the next one
                span = self.window.next_span(now)
                if not self.ledger.set_window_end(job_id, span.end):
                    return  # not queued any more: a duplicate of a job that already ran
                end = span.end
            start = self.window.next_span(end - timedelta(seconds=1)).start
            if now < start:
                raise RetryTask(eta=start.replace(tzinfo=None))
            window_end = end
        if not self.ledger.claim_running(job_id):
            return  # the same job reached us twice: the other task has it, or it is done
        if job.limit_key is not None and not self.slots.try_acquire(job.limit_key):
            self.ledger.mark_deferred(job_id)
            raise RetryTask(delay=RETRY_DELAY_S)  # asked again later, no thread kept waiting
        with self._idle:
            self._running += 1
        try:
            context = JobContext(
                job_id, job.kind, job.dedupe_key, job.scope_key, job.limit_key, window_end,
                job.cursor_index, self.clock, self.ledger, stop=lambda: self._stopping,
            )  # fmt: skip
            route[1](context)
        except JobFailedError as exc:
            self.ledger.mark_finished(job_id, error=exc.code, error_code=exc.code)
        except OSError as exc:
            if getattr(exc, "code", None) != "disk.low":  # DiskLowError: the disk filled mid-job
                self.ledger.mark_finished(job_id, error=type(exc).__name__, error_code="job.failed")
            else:
                self.ledger.park_running(job_id, "disk.low")
                self.pause_writes("disk.low")
        except Exception as exc:
            # the class name only: an upstream message may carry a title (EF-BIB-053)
            self.ledger.mark_finished(job_id, error=type(exc).__name__, error_code="job.failed")
        else:
            if context.preempted:
                self.ledger.mark_preempted(job_id)
            else:
                self.ledger.mark_finished(job_id)
        finally:
            if job.limit_key is not None:
                self.slots.release(job.limit_key)
            with self._idle:
                self._running -= 1
                self._idle.notify_all()


def boot_jobs(
    pools: JobPools,
    *,
    instance_lock: InstanceLock,
    resolve_intents: Callable[[], object],
    release_resource_locks: Callable[[], object] | None = None,
) -> BootReport:
    """Start-up, in this order, and only once the instance lock is ours (ENF-OPS-007, -015).

    0. `resolve_intents`: every open file-operation intent is settled before any job exists
    (EF-EXP-041); if it raises, nothing below runs. 1. `flush_locks` on every pool, 2. release the
    domain leases and the resource locks, 3. put the jobs that were `running` back (a killed
    worker is not acknowledged), 4. one scan: the catch-up of whatever came due during the
    downtime, once.
    """
    if not instance_lock.is_held():
        raise InstanceLockNotHeldError("flush_locks needs the instance lock")
    resolve_intents()
    if release_resource_locks is not None:
        release_resource_locks()
    flushed = pools.flush_huey_locks()
    released = pools.slots.release_all()
    requeued = pools.ledger.requeue_running()
    report = pools.scheduler.startup_scan()  # also sends the requeued rows
    return BootReport(flushed, released, len(requeued), len(report.queued))
