"""The record of an audit of Komga: what it proved, which shelves it paused, what it announced
(EF-EXP-047, ENF-SEC-016, M4 L08b). The adapter (`targets.komga.audit`) reads the server; this
module compares, writes and pauses, so that the targets never touch the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application import komga_audit_state as state
from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.target_state import (
    CHILD_EXPOSURE,
    load_health,
    pause_shelf,
    resume_shelf,
    save_health,
)
from shelfhold.export.domain.bindings import is_family
from shelfhold.export.domain.komga_access import (
    AUDIT_FAILED,
    BoundShelf,
    Finding,
    KomgaLibrary,
    evaluate,
)
from shelfhold.export.domain.path_map import PathMapError, clean_absolute
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export

__all__ = ["KomgaAuditRecorder", "KomgaAuditReport", "Reading", "cleaned"]


@dataclass
class KomgaAuditReport:
    proved: bool  # the shelf of the candidate (`admit`) or every bound shelf (`audit`)
    reason: str | None = None  # a closed code when not proved
    findings: tuple[Finding, ...] = ()
    paused_shelves: list[str] = field(default_factory=list)  # newly paused by this audit
    resumed_shelves: list[str] = field(default_factory=list)
    failed: bool = False  # the audit could not be completed
    error: str | None = None  # the class of the error, never its text
    version: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reading:
    version: str | None  # normalised, None when unreadable
    accounts: tuple[Any, ...] = ()
    libraries: tuple[KomgaLibrary, ...] = ()
    refusal: str | None = None  # a closed code: the release is not accepted
    advisories: tuple[str, ...] = ()


def cleaned(root: str | None) -> str | None:
    try:
        return clean_absolute(root) if root else None
    except PathMapError:
        return None


class KomgaAuditRecorder:
    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events

    def _bound(self, target_id: str) -> list[BoundShelf]:
        d, s, t = export.shelf_destination, export.shelf, export.export_target
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(d.c.shelf_id, d.c.root_subpath, s.c.max_age_rating, t.c.path_map)
                .select_from(d.join(s, s.c.id == d.c.shelf_id).join(t, t.c.id == d.c.target_id))
                .where(d.c.target_id == target_id)
                .order_by(d.c.id)
            ).all()
        return [
            BoundShelf(r.shelf_id, state.remote_root_of(r.path_map, r.root_subpath) or None,
                       r.max_age_rating)
            for r in rows
        ]  # fmt: skip

    def record(
        self, target_id: str, candidate: BoundShelf | None, reading: Reading | None,
        *, failed: str | None = None,
    ) -> KomgaAuditReport:  # fmt: skip
        bound = self._bound(target_id)
        shelves = [*bound, *([candidate] if candidate else [])]
        findings: tuple[Finding, ...] = ()
        reason: str | None = None
        must_pause: set[str] = set()
        complete = False
        if failed is not None or reading is None:
            reason = AUDIT_FAILED
            must_pause = {b.shelf_id for b in bound if b.shelf_id}  # nothing is known: all stop
        elif reading.refusal is not None:
            reason = reading.refusal
            must_pause = {b.shelf_id for b in bound if b.shelf_id and is_family(b.ceiling)}
        else:
            complete = True
            findings = evaluate(reading.accounts, reading.libraries, shelves)
            must_pause = {f.shelf_id for f in findings if f.shelf_id}
            mine = [f for f in findings if f.shelf_id is None]
            first = (mine or list(findings))[:1]
            reason = first[0].code if first else None
        proved = []
        if complete:
            bad = {f.shelf_id for f in findings}
            proved = [
                {"root": shelf.root, "ceiling": shelf.ceiling}
                for shelf in shelves
                if shelf.root and shelf.shelf_id not in bad
            ]
        if candidate is not None:  # the question is the shelf about to be linked
            proved_now = complete and None not in {f.shelf_id for f in findings}
        else:
            proved_now = complete and not findings
        report = KomgaAuditReport(
            proved=proved_now,
            reason=None if proved_now else reason,
            findings=findings,
            failed=failed is not None,
            error=failed,
            version=reading.version if reading else None,
            warnings=reading.advisories if reading else (),
        )
        with write_transaction(self._engine) as connection:
            previous = state.load(connection, target_id)
            ours = {s for s in previous.get("paused", []) if isinstance(s, str)}
            for shelf_id in sorted(must_pause):
                if pause_shelf(connection, shelf_id, CHILD_EXPOSURE):
                    report.paused_shelves.append(shelf_id)
                    ours.add(shelf_id)
            if complete:  # only a complete audit lifts, and only what this audit paused
                for shelf_id in sorted(ours - must_pause):
                    if resume_shelf(connection, shelf_id, CHILD_EXPOSURE):
                        report.resumed_shelves.append(shelf_id)
                    ours.discard(shelf_id)
            ours &= {b.shelf_id for b in bound if b.shelf_id}
            state.save(connection, target_id, {
                "audited_at": self._clock.now().isoformat(),
                "reason": reason,
                "proved": proved,
                "findings": [{"code": f.code, "library": f.library} for f in findings],
                "paused": sorted(ours),
                "version": report.version,
                "failed": failed is not None,
            })  # fmt: skip
            health = load_health(connection, target_id)
            if reading is not None:
                health.version = reading.version
                health.warnings = reading.advisories
            health.child_exposure = bool(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(export.shelf)
                    .where(export.shelf.c.paused_reason == CHILD_EXPOSURE)
                ).scalar_one()
            )
            health.refresh_waiting()
            save_health(connection, target_id, health)
        if report.paused_shelves:
            self._events.emit(
                "target.child_exposure", target_id=target_id, shelves=report.paused_shelves,
                accounts=[], cause=reason or AUDIT_FAILED,
            )  # fmt: skip
        return report
