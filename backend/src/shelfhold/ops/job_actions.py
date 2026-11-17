"""The reserved action that bringing a job back stands for (EF-EXP-030, M4 L34).

One place for `jobs retry` and `jobs replay`: the queue is never a way around the fresh session a
reserved order was given with.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.application.detach import DETACH_JOB_KIND
from shelfhold.export.application.remove_destination import REMOVAL_JOB_KIND
from shelfhold.infrastructure.schema import coherence, platform

APPLY_JOB_KIND = "export.apply"  # `runtime_jobs.APPLY_JOB_KIND` (the composition root)


def reserved_job_action(connection: Connection, subject: str) -> str | None:
    """The reserved action of putting this job back in the queue, or None.

    A job that applies a rename plan is `plan.apply_rename`. A removal that takes the files and a
    full detachment are the orders a fresh session gave with its phrase: neither comes back
    without that session, by `retry` or by `replay`."""
    job, plan = platform.job_view, coherence.export_plan
    row = connection.execute(
        sa.select(job.c.kind, job.c.dedupe_key).where(job.c.id == subject)
    ).first()
    if row is None:
        return None
    if row.kind == REMOVAL_JOB_KIND:
        return "destination.remove_all"
    if row.kind == DETACH_JOB_KIND:
        return "detach.full"
    if row.kind != APPLY_JOB_KIND or not row.dedupe_key:
        return None
    kind = connection.execute(
        sa.select(plan.c.kind).where(plan.c.plan_id == row.dedupe_key.split(":")[1])
    ).scalar()
    return "plan.apply_rename" if kind == "rename_plan" else None
