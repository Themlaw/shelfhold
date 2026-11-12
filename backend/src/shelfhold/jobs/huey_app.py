"""Huey, behind its adapter (ENF-GOV-002): one instance per pool, one periodic, no business logic.

Pools (02 section 3.5): `io` (network, several threads), `cpu` (one thread), `maint` (one thread,
the maintenance window). Each has its own SQLite file in `/data` (created by `ensure_volumes`), so
a long job in one pool never holds the lock another pool's queue needs (spike S1). The only
periodic Huey knows is `tick`, every minute, which calls `Scheduler.scan`; everything periodic is
a row of `schedule`.

Priorities: a Huey task carries the numeric priority of its class, `new` > `request` > `backfill`,
so a backlog (catch-up in batches) never delays a new chapter. Nothing starves: a `backfill` job
that has waited `BACKFILL_MAX_WAIT` is moved to the `request` class by the next tick, so the
delay of a catch-up is bounded by that wait plus the requests ahead of it.
"""

import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import structlog
from huey import SqliteHuey, crontab
from huey.api import Huey
from huey.consumer import Consumer
from huey.exceptions import RetryTask
from sqlalchemy import Engine
from structlog.contextvars import bound_contextvars

from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.logging import elapsed_ms, exception_detail
from shelfhold.jobs.control import (
    PAUSE_CODE,
    REPAIR_PRIORITY,
    SECURITY_REPAIR_KINDS,
    QueueControl,
)
from shelfhold.jobs.errors import JobFailedError
from shelfhold.jobs.ledger import (
    MAX_ATTEMPTS,
    RETRY_BACKOFF_S,
    Created,
    JobLedger,
    PendingJob,
)
from shelfhold.jobs.mode import PARK_CODE, MaintenanceMode
from shelfhold.jobs.scheduler import ScanReport, Scheduler
from shelfhold.jobs.slots import RETRY_DELAY_S, DomainSlots
from shelfhold.jobs.window import (
    DEFAULT_WINDOW,
    MaintenanceWindow,
    StoreOrigin,
    WindowRun,
    ensure_store_access,
    run_until,
)

log = structlog.get_logger(__name__)

POOLS = ("io", "cpu", "maint")
# Worker threads each pool's consumer is started with (`huey_consumer -w N -k thread`).
POOL_WORKERS = {"io": 4, "cpu": 1, "maint": 1}
CLASS_PRIORITY = {"new": 30, "request": 20, "backfill": 10}
BACKFILL_MAX_WAIT = timedelta(hours=6)  # EF-INT-017: the longest a catch-up batch waits as such
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
    counters: dict[str, object] = field(default_factory=dict)  # the result, in `job.finished`

    def record(self, **counters: object) -> None:
        """Counts a handler reports about its run; they come out in the `job.finished` line."""
        self.counters.update(counters)

    def add(self, **counts: int) -> None:
        """Adds to counts several steps of a run (one per destination) report."""
        for name, amount in counts.items():
            self.counters[name] = int(str(self.counters.get(name, 0))) + amount

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
        bus: EventBus | None = None,
        retry_backoff_s: tuple[float, ...] = RETRY_BACKOFF_S,
        freeze: FreezeState | None = None,
    ) -> None:
        self.clock = clock
        self._data_dir = data_dir
        self._backoff = retry_backoff_s  # the waits before the 2nd, 3rd... attempt (short in tests)
        self._bus = bus  # the operating bus: a dead letter is an event that notifies
        self.window = window
        self.ledger = JobLedger(engine, clock)
        self.slots = DomainSlots(engine, clock)
        self.mode = MaintenanceMode(engine, clock)  # EF-INT-018: the instance's own flag
        self.control = QueueControl(revoke=self.revoke)  # EF-INT-017
        self.scheduler = Scheduler(
            engine, clock=clock, seed=seed, dispatcher=self, window=window, freeze=freeze,
            mode=self.mode,
        )  # fmt: skip
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

    def queue_files(self) -> dict[str, Path]:
        """The SQLite file of each pool's queue, by name (`huey-io`...): upkeep treats them like the
        database (ENF-OPS-016)."""
        return {f"huey-{name}": self._data_dir / f"huey-{name}.db" for name in POOLS}

    def huey(self, pool: str) -> Huey:
        return self._hueys[pool]

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

    def promote_aged_backfill(self) -> int:
        """Send again, one class up, the `backfill` jobs that waited the bound (EF-INT-017). The
        first task stays in its queue: whichever reaches a worker first claims the job, the other
        finds it taken."""
        promoted = 0
        for job in self.ledger.aged_backfill(self.clock.now() - BACKFILL_MAX_WAIT):
            if self.ledger.promote(job.job_id, "request"):
                self.ledger.mark_dispatched(job.job_id, self.dispatch(self.ledger.get(job.job_id)))
                promoted += 1
        if promoted:
            log.info("jobs.backfill_promoted", count=promoted)
        return promoted

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

    def revoke(self, task_id: str) -> None:
        """The Huey task of a job that must not run as queued any more (a task not found is fine:
        a worker that gets it anyway finds the row not `queued` and leaves it.)"""
        for huey in self._hueys.values():
            huey.revoke_by_id(task_id)

    def end_maintenance_parks(self) -> int:
        """Once the mode is off, the jobs it parked go back to the queue (the tick and the boot
        call this, so the exit works from any process that wrote the flag)."""
        if self.mode.active():
            return 0
        return self._send_again(self.ledger.resume_paused(PARK_CODE))

    def _cancel_asked(self, job_id: str) -> bool:
        with self._engine.connect() as conn:
            return self.control.cancel_requested(conn, job_id)

    def _settle_cancel(self, job_id: str) -> bool:
        with self._engine.begin() as conn:
            return self.control.settle_cancelled(conn, job_id, now=self.clock.now())

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

    def forget_lost_dispatches(self) -> int:
        """At boot: a task Huey handed to a worker that was then killed is in no queue and no
        schedule, and its row says `queued`. It is sent again by the scan (`claim_running`
        makes a task that reaches Huey twice harmless)."""
        present: set[str] = set()
        for huey in self._hueys.values():
            present.update(str(t.id) for t in huey.pending())
            present.update(str(t.id) for t in huey.scheduled())
        return self.ledger.forget_dispatches_not_in(present)

    # -- shutdown (ENF-OPS-007) ---------------------------------------------------------------

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
        self.end_maintenance_parks()
        report = self.scheduler.scan()
        self.promote_aged_backfill()
        log.debug("scheduler.scanned", queued=len(report.queued))
        return report

    # -- Dispatcher -------------------------------------------------------------------------

    def dispatch(self, job: PendingJob) -> str:
        route = self._handlers.get(job.kind)
        if route is None:  # a schedule row from another version must not stop every scan
            self.ledger.mark_finished(job.job_id, error="no handler", error_code="job.unknown_kind")
            return f"unrouted:{job.job_id}"
        pool = route[0]
        priority = CLASS_PRIORITY.get(job.priority_class, CLASS_PRIORITY["new"])
        if job.kind in SECURITY_REPAIR_KINDS:
            priority = REPAIR_PRIORITY  # above every class a user can pick (EF-INT-017)
        task = self._run_job[pool].s(job.job_id, priority=priority)
        self._hueys[pool].enqueue(task)
        log.debug(
            "job.dispatched", job_id=job.job_id, job_kind=job.kind, pool=pool, priority=priority
        )
        return str(task.id)

    # -- the task body, run by a worker thread ----------------------------------------------

    def _execute(self, job_id: str) -> None:
        if self.scheduler.held:  # `restored_behind`: nothing runs until the adoption (EF-INT-026)
            raise RetryTask(delay=RETRY_DELAY_S)
        if self._stopping:
            self.ledger.forget_dispatch(job_id)  # the task is consumed: the next boot re-sends it
            log.debug("job.skipped", job_id=job_id, reason="stopping")
            return
        job = self.ledger.get(job_id)
        route = self._handlers.get(job.kind)
        if route is None:
            log.warning("job.unknown_kind", job_id=job_id, job_kind=job.kind)
            self.ledger.mark_finished(job_id, error="no handler", error_code="job.unknown_kind")
            return
        if self.mode.active():  # EF-INT-018: nothing starts, queued before or after the entry
            self.ledger.pause_one(job_id, PARK_CODE)
            log.debug("job.skipped", job_id=job_id, job_kind=job.kind, reason="maintenance")
            return  # parked, not failed, not retried: the exit puts it back
        with self._engine.connect() as conn:
            queue_paused = self.control.is_paused(conn, job.source_key)
        if queue_paused:  # EF-INT-017: a paused queue or source waits, it does not fail
            self.ledger.pause_one(job_id, PAUSE_CODE)
            log.debug("job.skipped", job_id=job_id, job_kind=job.kind, reason="queue_paused")
            return
        if route[3] and self._park_if_paused(job_id):
            log.debug("job.skipped", job_id=job_id, job_kind=job.kind, reason="writes_paused")
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
            # the one door of the periodic work to /store and /library: inside the window only
            ensure_store_access(StoreOrigin.PERIODIC, now=now, window=self.window)
            window_end = end
        if not self.ledger.claim_running(job_id):
            log.debug("job.skipped", job_id=job_id, job_kind=job.kind, reason="already_claimed")
            return  # the same job reached us twice: the other task has it, or it is done
        if job.limit_key is not None and not self.slots.try_acquire(job.limit_key):
            self.ledger.mark_deferred(job_id)
            raise RetryTask(delay=RETRY_DELAY_S)  # asked again later, no thread kept waiting
        with self._idle:
            self._running += 1
        retry_in: float | None = None
        started = time.perf_counter()
        outcome = "ok"
        context = JobContext(
            job_id, job.kind, job.dedupe_key, job.scope_key, job.limit_key, window_end,
            job.cursor_index, self.clock, self.ledger,
            stop=lambda: self._stopping or self._cancel_asked(job_id),
        )  # fmt: skip
        with bound_contextvars(job_id=job_id, job_kind=job.kind, pool=route[0]):
            log.info("job.started", attempts=self.ledger.attempts_of(job_id))
            try:
                route[1](context)
            except JobFailedError as exc:
                if self._settle_cancel(job_id):
                    outcome = "cancelled"
                else:
                    outcome = "failed"
                    self.ledger.mark_finished(job_id, error=exc.code, error_code=exc.code)
                    log.error("job.failed", error_code=exc.code)
            except OSError as exc:
                if self._settle_cancel(job_id):
                    outcome = "cancelled"
                elif getattr(exc, "code", None) != "disk.low":  # DiskLowError: the disk filled
                    outcome = "error"
                    retry_in = self._failed(job_id, job.kind, exc)
                else:
                    outcome = "parked"
                    self.ledger.park_running(job_id, "disk.low")
                    self.pause_writes("disk.low")
            except Exception as exc:
                if self._settle_cancel(job_id):
                    outcome = "cancelled"
                else:
                    outcome = "error"
                    retry_in = self._failed(job_id, job.kind, exc)
            else:
                if self._settle_cancel(job_id):
                    outcome = "cancelled"  # asked to stop: it ends cancelled, never succeeded
                elif context.preempted:
                    outcome = "preempted"
                    self.ledger.mark_preempted(job_id)
                else:
                    self.ledger.mark_finished(job_id)
            finally:
                if job.limit_key is not None:
                    self.slots.release(job.limit_key)
                with self._idle:
                    self._running -= 1
                    self._idle.notify_all()
                log.info(
                    "job.finished",
                    outcome=outcome,
                    duration_ms=elapsed_ms(started),
                    retry_in_s=retry_in,
                    **context.counters,
                )
        if retry_in is not None:
            raise RetryTask(delay=retry_in)  # Huey keeps the task and runs it again later

    def _failed(self, job_id: str, kind: str, exc: Exception) -> float | None:
        """A job raised. Attempts left: the wait before the next one. None: it gave up, and is a
        dead letter, announced on the operating bus in the same transaction (EF-INT-023)."""
        # the class name only: an upstream message may carry a title (EF-BIB-053)
        error = type(exc).__name__
        attempts = self.ledger.attempts_of(job_id)
        log.debug("job.failure_detail", **exception_detail(exc))  # DEBUG only, message escaped
        if attempts < MAX_ATTEMPTS:
            self.ledger.mark_retry(job_id, error)
            delay = self._backoff[min(attempts, len(self._backoff)) - 1]
            log.warning("job.retry", error=error, attempt=attempts, delay_s=delay)
            return delay
        log.error("job.dead_lettered", error=error, attempts=attempts)
        with self._engine.begin() as conn:
            self.ledger.mark_dead_letter(conn, job_id, error)
            if self._bus is not None:
                try:
                    self._bus.publish(
                        conn, "job.dead_lettered",
                        {"job_id": job_id, "job_kind": kind, "reason": "retries_exhausted"},
                    )  # fmt: skip
                except ValueError:  # a kind the bus cannot carry: the dead letter stays visible
                    log.warning("job.dead_letter_unannounced", job_id=job_id)
        return None


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
    pools.forget_lost_dispatches()  # taken from the queue by a worker that was killed
    pools.end_maintenance_parks()  # the mode may have been left while the server was down
    report = pools.scheduler.startup_scan()  # also sends the requeued rows
    return BootReport(flushed, released, len(requeued), len(report.queued))
