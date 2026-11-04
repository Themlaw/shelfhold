"""The scan of `schedule` (EF-INT-003, spike S1, ambiguity A8).

Huey's crontab is stateless and may skip a minute or see one twice, so nothing periodic is a
Huey crontab. One tick per minute (and one at boot) calls `Scheduler.scan`, which queues every
schedule whose `next_due_at` has passed. A row is claimed with a compare-and-swap on its old
`next_due_at`, in the same transaction that creates the `job_view` row and moves `next_due_at`
to the first occurrence after *now*: a minute seen twice finds nothing due, and an outage of
N periods costs one run, not N (run-once). The queue itself is written after the commit; a row
that never reached it is sent by the next scan.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from shelfhold.jobs.cron import CronSpec
from shelfhold.jobs.ledger import JobLedger, PendingJob
from shelfhold.jobs.window import DEFAULT_WINDOW, MaintenanceWindow, Span, window_rank

schedule = platform.schedule
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class Dispatcher(Protocol):
    def dispatch(self, job: PendingJob) -> str:
        """Put the job on its queue; return the queue's task id."""
        ...


def jitter_for(seed: str, kind: str, scope_key: str, base: datetime, jitter_s: int) -> int:
    """Seconds in [0, jitter_s], fixed for (instance seed, job, occurrence) so a restart agrees."""
    if jitter_s <= 0:
        return 0
    digest = hashlib.sha256(f"{seed}|{kind}|{scope_key}|{int(base.timestamp())}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (jitter_s + 1)


def _window_json(span: Span) -> dict[str, str]:
    return {"start_utc": span.start.strftime(_FORMAT), "end_utc": span.end.strftime(_FORMAT)}


def _span_of(window: dict[str, Any]) -> Span:
    def parse(text: str) -> datetime:
        return datetime.strptime(text, _FORMAT).replace(tzinfo=UTC)

    return Span(parse(window["start_utc"]), parse(window["end_utc"]))


@dataclass(frozen=True)
class ScanReport:
    queued: tuple[PendingJob, ...]


class Scheduler:
    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock,
        seed: str,
        dispatcher: Dispatcher,
        window: MaintenanceWindow = DEFAULT_WINDOW,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._seed = seed
        self._dispatcher = dispatcher
        self._window = window
        self._ledger = JobLedger(engine, clock)

    # -- registration ---------------------------------------------------------------------

    def add(
        self,
        kind: str,
        cron: str,
        *,
        scope_key: str = "",
        jitter_s: int = 0,
        windowed: bool = False,
        enabled: bool = True,
    ) -> None:
        """Register a periodic, or change its cron; its next due time is computed now."""
        CronSpec.parse(cron)  # refuse a bad expression at registration, not at 03:00
        now = self._clock.now()
        with self._engine.begin() as conn:
            conn.execute(
                sa.delete(schedule).where(
                    schedule.c.job_kind == kind, schedule.c.scope_key == scope_key
                )
            )
            due, window = self._plan(kind, scope_key, cron, jitter_s, windowed, now)
            conn.execute(
                sa.insert(schedule).values(
                    id=new_ulid(),
                    job_kind=kind,
                    scope_key=scope_key,
                    cron=cron,
                    jitter_s=jitter_s,
                    window=window,
                    enabled=enabled,
                    next_due_at=due,
                )
            )

    def _plan(
        self, kind: str, scope_key: str, cron: str, jitter_s: int, windowed: bool, after: datetime
    ) -> tuple[datetime, dict[str, str] | None]:
        """The next due instant after `after`, and the window it falls in if the job has one."""
        base = CronSpec.parse(cron).next_after(after)
        if not windowed:
            return base + timedelta(
                seconds=jitter_for(self._seed, kind, scope_key, base, jitter_s)
            ), None
        start = self._window.clamp(base)
        span = self._window.next_span(start)
        jitter = jitter_for(self._seed, kind, scope_key, start, jitter_s)
        due = min(start + timedelta(seconds=jitter), span.end - timedelta(seconds=1))
        return due, _window_json(span)

    # -- the scan ---------------------------------------------------------------------------

    def startup_scan(self) -> ScanReport:
        """The same scan, run once at boot: it is the catch-up (run-once) for the downtime."""
        return self.scan()

    def scan(self) -> ScanReport:
        now = self._clock.now()
        stranded = self._ledger.undispatched()  # queued by an earlier scan, never sent: first
        claimed = self._claim(now)
        for job in [*stranded, *claimed]:
            self._ledger.mark_dispatched(job.job_id, self._dispatcher.dispatch(job))
        return ScanReport(tuple(claimed))

    def _claim(self, now: datetime) -> list[PendingJob]:
        with self._engine.connect() as conn:
            due = conn.execute(
                sa.select(schedule)
                .where(schedule.c.enabled.is_(True), schedule.c.next_due_at <= now)
                .order_by(schedule.c.next_due_at)
            ).all()
        # window jobs go by their fixed priority, the others by how late they are
        ordered = sorted(
            due, key=lambda r: (window_rank(r.job_kind) if r.window else -1, r.next_due_at)
        )
        claimed: list[PendingJob] = []
        for row in ordered:
            job = self._claim_one(row, now)
            if job is not None:
                claimed.append(job)
        return claimed

    def _claim_one(self, row: "sa.Row[Any]", now: datetime) -> PendingJob | None:
        if row.window:
            span = _span_of(row.window)
            if now < span.start:
                return None  # not open yet (jitter is clamped into the window, so a safety net)
            if now >= span.end:
                # that night is over and nothing ran: no disk access by day, wait for the next
                self._advance(row, now, enqueue=False)
                return None
        return self._advance(row, now, enqueue=True)

    def _advance(self, row: "sa.Row[Any]", now: datetime, *, enqueue: bool) -> PendingJob | None:
        due, window = self._plan(
            row.job_kind, row.scope_key, row.cron, row.jitter_s, row.window is not None, now
        )
        with self._engine.begin() as conn:
            won = conn.execute(
                sa.update(schedule)
                .where(schedule.c.id == row.id, schedule.c.next_due_at == row.next_due_at)
                .values(
                    next_due_at=due,
                    window=window,
                    last_run_at=now if enqueue else row.last_run_at,
                )
            ).rowcount
            if not won or not enqueue:
                return None  # another scan claimed this occurrence first
            created = self._ledger.create(
                conn,
                kind=row.job_kind,
                dedupe_key=f"periodic:{row.job_kind}:{row.scope_key}",
                source_key=row.scope_key or None,
                priority_class="backfill" if row.window else "new",
            )
        if not created.created:
            return None  # the previous run of this periodic is still active: coalesced
        return PendingJob(
            job_id=created.job_id,
            kind=row.job_kind,
            dedupe_key=f"periodic:{row.job_kind}:{row.scope_key}",
            scope_key=row.scope_key,
            priority_class="backfill" if row.window else "new",
            window_end=_span_of(row.window).end if row.window else None,
        )
