"""Versioned plans (EF-EXP-042, I10): a preview is applied only if nothing moved since.

A plan carries `base_version` (the version counter of the Units, manifest and bindings it was
computed from) and the fingerprint `(path, size, mtime, inode)` of the files it touches. Applying
it compares both with the present, and the client's `If-Match` with the stored `base_version`; any
difference is `StalePlanError` (HTTP 412, "the preview changed, recompute"; 409 is for business
conflicts) and nothing is run.
"""

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence

__all__ = ["PlanOp", "PlanRecord", "PlanStore", "StalePlanError", "fingerprint_files"]


class StalePlanError(Exception):
    code = "plan.stale"
    status = 412


@dataclass(frozen=True)
class PlanOp:
    op: str
    src: str | None = None
    dst: str | None = None
    payload: dict[str, object] | None = None


@dataclass(frozen=True)
class PlanRecord:
    plan_id: str
    base_version: str
    fingerprint: str


def fingerprint_files(paths: Iterable[str]) -> str:
    """`sha256:` over `(path, size, mtime_ns, inode)` of each file, sorted; a missing one counts."""
    entries: list[list[object]] = []
    for path in sorted(set(paths)):
        try:
            observed = os.stat(path, follow_symlinks=False)
            entries.append([path, observed.st_size, observed.st_mtime_ns, observed.st_ino])
        except FileNotFoundError:
            entries.append([path, None, None, None])
    return "sha256:" + hashlib.sha256(json.dumps(entries).encode()).hexdigest()


class PlanStore:
    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock,
        current_version: Callable[[], str],
        step_version: Callable[[], str] | None = None,
    ) -> None:
        """`current_version` is compared at the claim; `step_version` (default: the same) before
        every operation, and may leave out what the operations themselves change."""
        self._engine = engine
        self._clock = clock
        self._current_version = current_version
        self._step_version = step_version or current_version

    def create(
        self,
        *,
        kind: str,
        files: Iterable[str],
        ops: Iterable[PlanOp],
        shelf_destination_id: str | None = None,
    ) -> PlanRecord:
        paths = list(files)
        record = PlanRecord(new_ulid(), self._current_version(), fingerprint_files(paths))
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(coherence.export_plan).values(
                    plan_id=record.plan_id,
                    shelf_destination_id=shelf_destination_id,
                    kind=kind,
                    dry_run=True,
                    base_version=record.base_version,
                    files_fingerprint=record.fingerprint,
                    batches=[{"batch": 1, "files": paths}],
                    status="planned",
                )
            )
            rows = [
                {
                    "id": new_ulid(),
                    "plan_id": record.plan_id,
                    "batch": 1,
                    "position": position,
                    "op": op.op,
                    "src_path": op.src,
                    "dst_path": op.dst,
                    "payload": op.payload,
                }
                for position, op in enumerate(ops)
            ]
            if rows:
                conn.execute(sa.insert(coherence.export_op), rows)
        return record

    def apply(
        self,
        plan_id: str,
        *,
        if_match: str,
        run: Callable[[PlanOp], None],
        lock: AbstractContextManager[object] | None = None,
    ) -> None:
        """Apply a fresh preview once. `lock` is the job's `ResourceLocks.hold(...)`.

        The plan is *claimed* by one conditional UPDATE (`planned` and the client's `base_version`
        become `applying`, one row or nothing), so a second concurrent application is refused;
        the version is read again before every operation, and a counter that moved stops the plan
        (412) with what already ran left to the intent journal.
        """
        with lock if lock is not None else nullcontext():
            self._apply_claimed(plan_id, if_match, run)

    def _apply_claimed(self, plan_id: str, if_match: str, run: Callable[[PlanOp], None]) -> None:
        plan_table, op_table = coherence.export_plan, coherence.export_op
        with self._engine.connect() as conn:
            plan = conn.execute(sa.select(plan_table).where(plan_table.c.plan_id == plan_id)).one()
            op_rows = conn.execute(
                sa.select(op_table)
                .where(op_table.c.plan_id == plan_id)
                .order_by(op_table.c.batch, op_table.c.position)
            ).all()
        files = [f for batch in plan.batches for f in batch.get("files", [])]
        if (
            plan.status != "planned"
            or if_match != plan.base_version
            or self._current_version() != plan.base_version
            or fingerprint_files(files) != plan.files_fingerprint
        ):
            self._set_plan(plan_id, "stale", only_from="planned")
            raise StalePlanError("the preview changed: recompute it")
        with self._engine.begin() as conn:  # the claim
            claimed = conn.execute(
                sa.update(plan_table)
                .where(
                    plan_table.c.plan_id == plan_id,
                    plan_table.c.status == "planned",
                    plan_table.c.base_version == if_match,
                )
                .values(status="applying", updated_at=self._clock.now())
            ).rowcount
        if claimed != 1:  # another application got there first
            raise StalePlanError("the plan is already being applied or was applied")
        baseline = self._step_version()
        for row in op_rows:
            if self._step_version() != baseline:
                self._set_plan(plan_id, "stale", only_from="applying")
                raise StalePlanError("the data changed while the plan was being applied")
            op = PlanOp(row.op, row.src_path, row.dst_path, row.payload)
            try:
                run(op)
            except BaseException:
                self._set_op(row.id, "failed")
                self._set_plan(plan_id, "failed", only_from="applying")
                raise
            self._set_op(row.id, "done")
        self._set_plan(plan_id, "done", only_from="applying")

    def _set_plan(self, plan_id: str, status: str, *, only_from: str) -> None:
        table = coherence.export_plan
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(table)
                .where(table.c.plan_id == plan_id, table.c.status == only_from)
                .values(status=status, updated_at=self._clock.now())
            )

    def _set_op(self, op_id: str, status: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(coherence.export_op)
                .where(coherence.export_op.c.id == op_id)
                .values(status=status)
            )
