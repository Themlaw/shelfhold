"""Technical retention of the database (ENF-OPS-009), as plain functions over an engine.

- events and finished jobs: 90 days by default (`retention_events_days`);
- audit: one year by default (`retention_audit_days`, never under 90 days).

Only rows end here, never a file. A job that waits for a human (`dead_letter`, `paused`) or
is queued or running is not history and stays whatever its age. The notification queue follows
its event (`ON DELETE CASCADE`). The audit of a deleted user is not shortened: it is
pseudonymised (`SqlIdentityStore.delete_user`) and then ages like any other line.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import identity, platform

FINISHED_JOB_STATUSES = ("succeeded", "failed", "cancelled")
BATCH = 5000  # rows per transaction: a long purge never holds the write lock for long


@dataclass(frozen=True)
class RetentionPolicy:
    events_days: int = 90
    jobs_days: int = 90
    audit_days: int = 365

    @classmethod
    def from_settings(cls, settings: Settings) -> RetentionPolicy:
        return cls(
            settings.retention_events_days,
            settings.retention_events_days,
            settings.retention_audit_days,
        )


@dataclass(frozen=True)
class ExpiredHistory:
    events: int
    jobs: int


def _note_purged_events(conn: sa.Connection, ids: Sequence[Any]) -> None:
    """The highest event id that goes, in the transaction that deletes it: a `?since=` token under
    it can no longer be answered (M3 L11)."""
    conn.execute(
        sa.update(platform.instance).values(
            events_purged_through=sa.func.max(platform.instance.c.events_purged_through, max(ids))
        )
    )


def _delete_older(
    engine: Engine,
    table: sa.Table,
    key: sa.Column[object],
    where: object,
    on_batch: Callable[[sa.Connection, Sequence[Any]], None] | None = None,
) -> int:
    total = 0
    while True:
        with engine.begin() as conn:
            ids = conn.execute(sa.select(key).where(where).limit(BATCH)).scalars().all()  # type: ignore[arg-type]
            if not ids:
                return total
            if on_batch is not None:
                on_batch(conn, ids)
            total += conn.execute(sa.delete(table).where(key.in_(ids))).rowcount
        if len(ids) < BATCH:
            return total


def expire_events_and_jobs(
    engine: Engine, *, now: datetime, policy: RetentionPolicy
) -> ExpiredHistory:
    event, job = platform.event, platform.job_view
    events = _delete_older(
        engine, event, event.c.id, event.c.occurred_at < now - timedelta(days=policy.events_days),
        _note_purged_events,
    )  # fmt: skip
    jobs = _delete_older(
        engine, job, job.c.id,
        sa.and_(
            job.c.status.in_(FINISHED_JOB_STATUSES),
            job.c.finished_at.is_not(None),
            job.c.finished_at < now - timedelta(days=policy.jobs_days),
        ),
    )  # fmt: skip
    return ExpiredHistory(events, jobs)


def expire_audit(engine: Engine, *, now: datetime, policy: RetentionPolicy) -> int:
    audit = identity.audit
    # Owner decision 2026-11-12: a passkey registration the admin has not acknowledged is never
    # expired, however old: the line of `status` is read from it.
    acknowledged = (
        sa.select(sa.func.coalesce(sa.func.max(audit.c.target), ""))
        .where(audit.c.action == "passkey.registrations_acknowledged")
        .scalar_subquery()
    )
    waiting = sa.and_(audit.c.action == "passkey.registered", audit.c.id > acknowledged)
    return _delete_older(
        engine,
        audit,
        audit.c.id,
        sa.and_(audit.c.at < now - timedelta(days=policy.audit_days), sa.not_(waiting)),
    )
