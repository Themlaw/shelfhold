"""Control of the queue by hand (EF-INT-017): pause, resume, cancel, priority, replay.

Everything here is a change of `job_view` (and of `app_setting` for a pause), made on the caller's
connection so that the audit line commits with it; the workers read the same rows, so nothing here
needs a handle on the running pools. A row put back in the queue gets `huey_task_id = NULL`: the
next scan sends it (`JobLedger.undispatched`), which is also what makes the commands work on a
stopped server. The optional `revoke` drops the Huey task of a job that must no longer run.

- **Pause** (`queue.paused`), global or for one source (`source_key`): the jobs queued now wait
  without failing, and a job that arrives later (or reaches a worker later) waits as well. A job
  already running goes on. `resume` puts back only the jobs this pause parked: the jobs parked by
  the window (`window.preempted`), the disk guard or the maintenance mode keep their own reason.
- **Cancel**: a job that waits is cancelled at once. A running one whose kind stops between steps
  (`jobs.kinds`) is asked to stop (a marker the worker reads, like SIGTERM): it ends `cancelled`
  when its handler returns, its locks and its domain slot released then, and a retry never starts
  it again. Any other kind is left to finish and the answer is `too_late`. The marker belongs to the
  run it was asked in: the next claim and a replay both clear it.
- **Priority**: one of the three user classes. A security repair (`jobs.kinds`) is above
  all of them at every moment: its priority cannot be changed, no class a user can pick reaches it,
  and a pause does not park it.
- **Replay**: a failed, cancelled or dead-lettered job goes back to the queue as the same row, so
  that replaying twice is one job; if another job holds its dedupe key now, that one is the answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.requesters import still_a_user
from shelfhold.infrastructure.schema import coherence, platform
from shelfhold.jobs.kinds import security_repair_kinds, traits_of
from shelfhold.jobs.ledger import ACTIVE, CANCEL_PREFIX, PAUSE_PREFIX

__all__ = [
    "CANCEL_PREFIX",
    "PAUSE_CODE",
    "PAUSE_PREFIX",
    "REPAIR_PRIORITY",
    "SECURITY_REPAIR_KINDS",
    "USER_CLASSES",
    "CancelOutcome",
    "JobNotFoundError",
    "JobStateError",
    "PriorityRefusedError",
    "QueueControl",
]

PAUSE_CODE: Final = "queue.paused"  # `job_view.error_code` of a job parked by a pause
USER_CLASSES: Final = ("new", "request", "backfill")  # what a user may pick, highest first
REPAIR_PRIORITY: Final = 50  # above CLASS_PRIORITY["new"] (30): no class reaches it
SECURITY_REPAIR_KINDS: Final = security_repair_kinds()  # declared in `jobs.kinds`, with the kinds
_FINAL = ("failed", "cancelled", "dead_letter")  # what a replay takes back

job_view = platform.job_view
app_setting = platform.app_setting


class JobNotFoundError(LookupError):
    pass


class JobStateError(ValueError):
    """The job is not in a state the command applies to."""


class PriorityRefusedError(ValueError):
    code = "queue.priority_locked"


@dataclass(frozen=True)
class CancelOutcome:
    job_id: str
    outcome: str  # "cancelled", "cancelling" (it stops at its next step) or "too_late"


Scope = tuple[str, str | None]  # ("all", None) or ("source", key): a source called `all` is one
ALL: Final[Scope] = ("all", None)


def _scope_key(source: str | None) -> str:
    return f"{PAUSE_PREFIX}all" if source is None else f"{PAUSE_PREFIX}source.{source}"


class QueueControl:
    def __init__(self, *, revoke: Callable[[str], None] | None = None) -> None:
        self._revoke = revoke

    # -- pause and resume -----------------------------------------------------------------------

    def pause(self, conn: Connection, *, source: str | None, at: datetime, by: str | None) -> int:
        """The number of jobs parked now; the pause itself is recorded even when it is 0."""
        by = still_a_user(conn, by)  # the admin may be gone since: the pause is then nobody's
        value = json.dumps({"since": at.isoformat(), "by": by})
        conn.execute(
            sqlite_insert(app_setting)
            .values(key=_scope_key(source), value=value)
            .on_conflict_do_nothing(index_elements=["key"])  # the first pause keeps its date
        )
        where = [job_view.c.status == "queued", job_view.c.kind.not_in(SECURITY_REPAIR_KINDS)]
        if source is not None:
            where.append(job_view.c.source_key == source)
        return int(
            conn.execute(
                sa.update(job_view).where(*where).values(status="paused", error_code=PAUSE_CODE)
            ).rowcount
        )

    def resume(self, conn: Connection, *, source: str | None) -> int:
        """Lift the pause of this scope and put back the jobs it parked. `source=None` lifts every
        pause (the whole queue); a source resumed under a global pause stays paused."""
        if source is None:
            conn.execute(sa.delete(app_setting).where(app_setting.c.key.like(f"{PAUSE_PREFIX}%")))
            where = [job_view.c.status == "paused", job_view.c.error_code == PAUSE_CODE]
        else:
            conn.execute(sa.delete(app_setting).where(app_setting.c.key == _scope_key(source)))
            if ALL in self.paused_scopes(conn):
                return 0
            where = [
                job_view.c.status == "paused",
                job_view.c.error_code == PAUSE_CODE,
                job_view.c.source_key == source,
            ]
        return int(
            conn.execute(
                sa.update(job_view)
                .where(*where)
                .values(status="queued", error_code=None, huey_task_id=None)
            ).rowcount
        )

    @staticmethod
    def paused_scopes(conn: Connection) -> set[Scope]:
        """`("all", None)` and the `("source", key)` that are paused."""
        found: set[Scope] = set()
        for key in conn.execute(
            sa.select(app_setting.c.key).where(app_setting.c.key.like(f"{PAUSE_PREFIX}%"))
        ).scalars():
            rest = key[len(PAUSE_PREFIX) :]
            if rest == "all":
                found.add(ALL)
            elif rest.startswith("source."):
                found.add(("source", rest[len("source.") :]))
        return found

    def is_paused(self, conn: Connection, source_key: str | None, kind: str | None = None) -> bool:
        """A security repair is never parked by a pause (EF-INT-017): only the mode stops it."""
        if kind is not None and kind in SECURITY_REPAIR_KINDS:
            return False
        scopes = self.paused_scopes(conn)
        return ALL in scopes or (source_key is not None and ("source", source_key) in scopes)

    # -- cancel -------------------------------------------------------------------------------

    def cancel(self, conn: Connection, job_id: str, *, now: datetime) -> CancelOutcome:
        row = self._row(conn, job_id)
        if row.status in ("queued", "paused"):
            conn.execute(
                sa.update(job_view)
                .where(job_view.c.id == job_id, job_view.c.status.in_(("queued", "paused")))
                .values(status="cancelled", finished_at=now, started_at=None, error_code=None)
            )
            self.release(conn, job_id)
            if row.huey_task_id and self._revoke is not None:
                self._revoke(row.huey_task_id)
            return CancelOutcome(job_id, "cancelled")
        if row.status == "running":
            if not traits_of(row.kind).cancellable:
                return CancelOutcome(job_id, "too_late")  # it never looks: it finishes, as it is
            # the statement that writes checks the status: a run that just ended gets no marker
            conn.execute(
                sa.text(
                    "INSERT INTO app_setting (key, value) SELECT :key, :value "
                    "WHERE EXISTS (SELECT 1 FROM job_view WHERE id = :id AND status = 'running') "
                    "ON CONFLICT (key) DO NOTHING"
                ),
                {"key": f"{CANCEL_PREFIX}{job_id}", "value": now.isoformat(), "id": job_id},
            )
            return CancelOutcome(job_id, "cancelling")
        raise JobStateError(f"a job that is {row.status} cannot be cancelled")

    @staticmethod
    def cancel_requested(conn: Connection, job_id: str) -> bool:
        return (
            conn.execute(
                sa.select(app_setting.c.key).where(app_setting.c.key == f"{CANCEL_PREFIX}{job_id}")
            ).first()
            is not None
        )

    @staticmethod
    def settle_cancelled(conn: Connection, job_id: str, *, now: datetime) -> bool:
        """The handler of a cancelled job returned (or raised): the row ends `cancelled`, the
        marker goes, and what the job held is released. False when no cancel was asked."""
        marker = conn.execute(
            sa.delete(app_setting).where(app_setting.c.key == f"{CANCEL_PREFIX}{job_id}")
        ).rowcount
        if not marker:
            return False
        conn.execute(
            sa.update(job_view)
            .where(job_view.c.id == job_id)
            .values(status="cancelled", finished_at=now, started_at=None, error_code=None)
        )
        QueueControl.release(conn, job_id)
        return True

    @staticmethod
    def release(conn: Connection, job_id: str) -> None:
        """What a cancelled job may still hold: the resource locks taken under its id (a handler
        releases them on its way out; this is for the one that died before). Its domain slot is
        returned by the worker, which owns the lease. File-operation intents are not tied to a
        job: a step is never interrupted, so none is left open by a cancel (EF-EXP-041)."""
        conn.execute(
            sa.delete(coherence.resource_lock).where(
                coherence.resource_lock.c.holder_job_id == job_id
            )
        )

    # -- priority -----------------------------------------------------------------------------

    def set_priority(self, conn: Connection, job_id: str, priority_class: str) -> str:
        if priority_class not in USER_CLASSES:
            raise PriorityRefusedError(f"{priority_class!r} is not a class a user may choose")
        row = self._row(conn, job_id)
        if row.kind in SECURITY_REPAIR_KINDS:
            raise PriorityRefusedError("a security repair keeps its place above every class")
        if row.status != "queued":
            raise JobStateError("only a job that waits has a priority to change")
        if row.priority_class != priority_class:
            conn.execute(
                sa.update(job_view)
                .where(job_view.c.id == job_id, job_view.c.status == "queued")
                .values(priority_class=priority_class, huey_task_id=None)
            )  # sent again by the next scan, at its new priority
            if row.huey_task_id and self._revoke is not None:
                self._revoke(row.huey_task_id)
        return priority_class

    # -- replay -------------------------------------------------------------------------------

    def replay(self, conn: Connection, job_id: str) -> tuple[str, bool]:
        """`(id, replayed)`: the job that is queued after the call (this one, or the active job
        that holds its dedupe key) and whether this call queued it. Replaying again changes
        nothing."""
        row = self._row(conn, job_id)
        if row.status in ACTIVE:
            return job_id, False  # already waiting or running: the second replay is a no-op
        if row.status not in _FINAL:
            raise JobStateError(f"a job that is {row.status} cannot be replayed")
        if row.dedupe_key is not None:
            holder = conn.execute(
                sa.select(job_view.c.id).where(
                    job_view.c.dedupe_key == row.dedupe_key, job_view.c.status.in_(ACTIVE)
                )
            ).scalar()
            if holder is not None:
                return str(holder), False
        conn.execute(sa.delete(app_setting).where(app_setting.c.key == f"{CANCEL_PREFIX}{job_id}"))
        conn.execute(
            sa.update(job_view)
            .where(job_view.c.id == job_id, job_view.c.status.in_(_FINAL))
            .values(
                status="queued", huey_task_id=None, attempts=0, last_error=None, error_code=None,
                started_at=None, finished_at=None, cursor=None,
            )
        )  # fmt: skip
        return job_id, True

    # -- helpers ------------------------------------------------------------------------------

    @staticmethod
    def _row(conn: Connection, job_id: str) -> sa.Row[tuple[object, ...]]:
        row = conn.execute(sa.select(job_view).where(job_view.c.id == job_id)).first()
        if row is None:
            raise JobNotFoundError(job_id)
        return row
