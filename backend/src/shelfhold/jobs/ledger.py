"""`job_view`: the Shelfhold side of every job (status, dedupe key, who waits for what).

Huey forgets a task the moment a worker takes it, so a row here is what survives a `kill -9`: a
job found `running` at boot goes back to the queue. The idempotency unit is the dedupe key (for a
download, the chapter): at most one active row per key (partial unique index).
"""

import json
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform

job_view = platform.job_view
ACTIVE = ("queued", "running", "paused")
PREEMPTED = "window.preempted"  # `error_code` of a window job stopped at the end of its window


@dataclass(frozen=True)
class PendingJob:
    """What a dispatcher needs to put a job on a queue."""

    job_id: str
    kind: str
    dedupe_key: str
    scope_key: str = ""
    source_key: str | None = None
    limit_key: str | None = None
    priority_class: str = "new"
    window_end: datetime | None = None  # a window job must stop by then (persisted, 0005)
    cursor_index: int = 0  # where a preempted window job resumes (persisted, 0005)


@dataclass(frozen=True)
class Created:
    job_id: str
    created: bool  # False: an active job with the same dedupe key already existed
    resumed: bool = False  # that job was a preempted window job, now queued again


class JobLedger:
    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    def create(
        self,
        conn: Connection,
        *,
        kind: str,
        dedupe_key: str,
        source_key: str | None = None,
        limit_key: str | None = None,
        priority_class: str = "new",
        window_end: datetime | None = None,
    ) -> Created:
        existing = self._active(conn, dedupe_key)
        if existing is not None:
            if window_end is not None and self._resume(conn, existing, window_end):
                return Created(existing, created=False, resumed=True)
            return Created(existing, created=False)
        job_id = new_ulid()
        try:
            with conn.begin_nested():
                conn.execute(
                    sa.insert(job_view).values(
                        id=job_id,
                        kind=kind,
                        status="queued",
                        dedupe_key=dedupe_key,
                        source_key=source_key,
                        limit_key=limit_key,
                        priority_class=priority_class,
                        window_end=window_end,
                        created_at=self._clock.now(),
                    )
                )
        except sa.exc.IntegrityError:  # another writer got there between our read and write
            raced = self._active(conn, dedupe_key)
            if raced is None:
                raise
            return Created(raced, created=False)
        return Created(job_id, created=True)

    @staticmethod
    def _resume(conn: Connection, job_id: str, window_end: datetime) -> bool:
        """A window job preempted last night goes back to the queue for tonight's window, with
        its cursor: the occurrence that is due now is that continuation, not a second job."""
        return bool(
            conn.execute(
                sa.update(job_view)
                .where(
                    job_view.c.id == job_id,
                    job_view.c.status == "paused",
                    job_view.c.error_code == PREEMPTED,
                )
                .values(
                    status="queued",
                    error_code=None,
                    huey_task_id=None,
                    window_end=window_end,
                )
            ).rowcount
        )

    @staticmethod
    def _active(conn: Connection, dedupe_key: str) -> str | None:
        return conn.execute(
            sa.select(job_view.c.id).where(
                job_view.c.dedupe_key == dedupe_key, job_view.c.status.in_(ACTIVE)
            )
        ).scalar_one_or_none()

    def mark_dispatched(self, job_id: str, huey_task_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(job_view).where(job_view.c.id == job_id).values(huey_task_id=huey_task_id)
            )

    def claim_running(self, job_id: str) -> bool:
        """`queued` -> `running`, once: False when another task of the same job got there first
        (a job can reach Huey twice, e.g. a crash between enqueue and `mark_dispatched`)."""
        with self._engine.begin() as conn:
            return bool(
                conn.execute(
                    sa.update(job_view)
                    .where(job_view.c.id == job_id, job_view.c.status == "queued")
                    .values(
                        status="running",
                        started_at=self._clock.now(),
                        attempts=job_view.c.attempts + 1,
                    )
                ).rowcount
            )

    def set_window_end(self, job_id: str, window_end: datetime) -> bool:
        """Move a still-queued window job to another window; False if it is not queued."""
        with self._engine.begin() as conn:
            return bool(
                conn.execute(
                    sa.update(job_view)
                    .where(job_view.c.id == job_id, job_view.c.status == "queued")
                    .values(window_end=window_end)
                ).rowcount
            )

    def save_cursor(self, job_id: str, next_index: int | None) -> None:
        """Keep (or, with None, clear) where the job resumes."""
        value = None if next_index is None else json.dumps({"next_index": next_index})
        with self._engine.begin() as conn:
            conn.execute(sa.update(job_view).where(job_view.c.id == job_id).values(cursor=value))

    def mark_preempted(self, job_id: str) -> None:
        """The window closed before the job was done: parked, with its cursor, until the next."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(job_view)
                .where(job_view.c.id == job_id)
                .values(status="paused", error_code=PREEMPTED, started_at=None, window_end=None)
            )

    def pause_queued(self, code: str, kinds: Collection[str]) -> int:
        """Queue-wide pause (disk guard): queued jobs of these kinds park with `code`. No attempt
        is spent and nothing is rescheduled; `resume_paused` puts them back."""
        with self._engine.begin() as conn:
            return int(
                conn.execute(
                    sa.update(job_view)
                    .where(job_view.c.status == "queued", job_view.c.kind.in_(list(kinds)))
                    .values(status="paused", error_code=code)
                ).rowcount
            )

    def pause_one(self, job_id: str, code: str) -> bool:
        with self._engine.begin() as conn:
            return bool(
                conn.execute(
                    sa.update(job_view)
                    .where(job_view.c.id == job_id, job_view.c.status == "queued")
                    .values(status="paused", error_code=code)
                ).rowcount
            )

    def park_running(self, job_id: str, code: str) -> bool:
        """A job that hit a pause condition mid-flight (the disk filled): back to `paused`
        under `code`, the attempt it spent given back, nothing recorded as a failure."""
        with self._engine.begin() as conn:
            return bool(
                conn.execute(
                    sa.update(job_view)
                    .where(job_view.c.id == job_id, job_view.c.status == "running")
                    .values(
                        status="paused",
                        error_code=code,
                        started_at=None,
                        attempts=sa.case(
                            (job_view.c.attempts > 0, job_view.c.attempts - 1), else_=0
                        ),
                    )
                ).rowcount
            )

    def forget_dispatch(self, job_id: str) -> None:
        """The Huey task was consumed without running the job (shutdown): `undispatched()` will
        send it again at the next boot."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(job_view)
                .where(job_view.c.id == job_id, job_view.c.status == "queued")
                .values(huey_task_id=None)
            )

    def resume_paused(self, code: str, job_id: str | None = None) -> list[PendingJob]:
        """The jobs parked with `code` go back to the queue, to be sent again."""
        with self._engine.begin() as conn:
            where: tuple[object, ...] = (
                job_view.c.status == "paused", job_view.c.error_code == code,
                *((job_view.c.id == job_id,) if job_id else ()),
            )  # fmt: skip
            rows = conn.execute(sa.select(job_view).where(*where)).all()  # type: ignore[arg-type]
            conn.execute(
                sa.update(job_view)
                .where(*where)  # type: ignore[arg-type]
                .values(status="queued", error_code=None, huey_task_id=None)
            )
        return [self._pending(r) for r in rows]

    def paused_reasons(self) -> dict[str, str]:
        """Kind -> the code a queue-wide pause parked it with."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(job_view.c.kind, job_view.c.error_code).where(
                    job_view.c.status == "paused", job_view.c.error_code.is_not(None)
                )
            )
            return {str(r.kind): str(r.error_code) for r in rows}

    def mark_finished(
        self, job_id: str, *, error: str | None = None, error_code: str | None = None
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(job_view)
                .where(job_view.c.id == job_id)
                .values(
                    status="failed" if error_code or error else "succeeded",
                    finished_at=self._clock.now(),
                    last_error=error,
                    error_code=error_code,
                )
            )

    def mark_deferred(self, job_id: str) -> None:
        """Back to `queued` (a busy domain slot): the task will be retried later."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(job_view)
                .where(job_view.c.id == job_id)
                .values(status="queued", started_at=None)
            )

    def undispatched(self) -> list[PendingJob]:
        """Queued rows that never reached Huey: a crash between commit and enqueue."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(job_view)
                .where(job_view.c.status == "queued", job_view.c.huey_task_id.is_(None))
                .order_by(job_view.c.id)
            ).all()
        return [self._pending(r) for r in rows]

    def requeue_running(self) -> list[PendingJob]:
        """At boot: a job still `running` lost its worker; it goes back to the queue."""
        with self._engine.begin() as conn:
            rows = conn.execute(sa.select(job_view).where(job_view.c.status == "running")).all()
            conn.execute(
                sa.update(job_view)
                .where(job_view.c.status == "running")
                .values(status="queued", huey_task_id=None, started_at=None)
            )
        return [self._pending(r) for r in rows]

    def get(self, job_id: str) -> PendingJob:
        with self._engine.connect() as conn:
            row = conn.execute(sa.select(job_view).where(job_view.c.id == job_id)).one()
        return self._pending(row)

    @staticmethod
    def _pending(row: "sa.Row[tuple[object, ...]]") -> PendingJob:
        return PendingJob(
            job_id=row.id,
            kind=row.kind,
            dedupe_key=row.dedupe_key or row.id,
            scope_key=row.source_key or "",
            source_key=row.source_key,
            limit_key=row.limit_key,
            priority_class=row.priority_class,
            window_end=row.window_end,
            cursor_index=int(json.loads(row.cursor)["next_index"]) if row.cursor else 0,
        )
