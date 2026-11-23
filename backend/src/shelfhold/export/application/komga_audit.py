"""The record of an audit of Komga: what it proved, which shelves it paused, what it announced
(EF-EXP-047, ENF-SEC-016, M4 L08b). The adapter (`targets.komga.audit`) reads the server; this
module compares, writes and pauses, so that the targets never touch the database.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application import komga_audit_state as state
from shelfhold.export.application.exposure_holds import held_by, hold, other_targets_of, release
from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.target_state import (
    CHILD_EXPOSURE,
    load_health,
    save_health,
)
from shelfhold.export.domain.bindings import family_clause, is_family
from shelfhold.export.domain.komga_access import (
    AUDIT_FAILED,
    AUDIT_SUPERSEDED,
    BoundShelf,
    Finding,
    KomgaLibrary,
    child_reach,
    evaluate,
)
from shelfhold.export.domain.path_map import PathMapError, clean_absolute, within
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export

__all__ = ["KomgaAuditRecorder", "KomgaAuditReport", "Reading", "Stamp", "cleaned", "new_stamp"]

_SEQUENCE = itertools.count(1)
Stamp = tuple[str, int]  # (instant, sequence): when a reading began, and in which order


def new_stamp(clock: Clock) -> Stamp:
    """Taken **before** the first request of a reading. The sequence orders two readings begun in
    the same instant; across a restart the instant does (#320 M1)."""
    return (clock.now().isoformat(), next(_SEQUENCE))


def _key(stamp: Any) -> tuple[datetime, int] | None:
    try:
        return datetime.fromisoformat(stamp[0]), int(stamp[1])
    except (TypeError, ValueError, IndexError):
        return None


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
    folders: int = 0  # the folders the reading was compared for: `proved` says nothing without one


@dataclass(frozen=True)
class Reading:
    version: str | None  # normalised, None when unreadable
    accounts: tuple[Any, ...] = ()
    libraries: tuple[KomgaLibrary, ...] = ()
    refusal: str | None = None  # a closed code: the release is not accepted
    advisories: tuple[str, ...] = ()
    stamp: Stamp | None = None


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
        state.use_clock(clock)  # the gate reads the freshness of the proof on this clock

    @staticmethod
    def _bound(connection: Any, target_id: str) -> list[BoundShelf]:
        d, s, t = export.shelf_destination, export.shelf, export.export_target
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

    @staticmethod
    def _kept(previous: dict[str, Any]) -> list[BoundShelf]:
        """The folders the state proved before, with the ceiling each was proved for: the day's
        audit keeps their proof fresh — a binding refused for a stale one is reopened by the
        audit of the day, not by a new link alone (L22)."""
        kept: list[BoundShelf] = []
        for entry in previous.get("proved") or []:
            if not (isinstance(entry, dict) and isinstance(entry.get("root"), str)):
                continue
            ceiling = entry.get("ceiling")
            kept.append(
                BoundShelf(None, entry["root"], ceiling if isinstance(ceiling, int) else None)
            )
        return kept

    @staticmethod
    def _pending(connection: Any, target_id: str) -> list[BoundShelf]:
        """The folders of the links that wait: the folders the shelves below 18+ write (the
        trees a Komga indexes, ADR-0052), under the folder the first writable entry of this
        server's path map maps — the one a binding is written against (`remote_root_of`), so
        never a folder under another entry. The day's audit is the production door that proves
        them (L22): a family shelf is refused only until the next audit, never forever. The
        shelf is not bound to this server, so a finding withdraws the proof (the link is
        refused with its reason) but pauses nothing."""
        path_map = connection.execute(
            sa.select(export.export_target.c.path_map).where(export.export_target.c.id == target_id)
        ).scalar()
        if not isinstance(path_map, list):
            return []
        writable = [
            entry
            for entry in path_map
            if isinstance(entry, dict) and entry.get("mode", "rw") == "rw"
        ]
        if not writable:
            return []
        # the entry a binding of this server is written against (`remote_root_of` takes the
        # first): a folder under another entry is a folder no binding can be asked about
        local, remote = writable[0].get("local"), writable[0].get("remote")
        if not isinstance(local, str) or not isinstance(remote, str):
            return []
        d, s, t = export.shelf_destination, export.shelf, export.export_target
        rows = connection.execute(
            sa.select(d.c.root_subpath, t.c.path_map, s.c.max_age_rating)
            .select_from(d.join(s, s.c.id == d.c.shelf_id).join(t, t.c.id == d.c.target_id))
            .where(
                d.c.target_id != target_id,
                d.c.transport == "tree",
                family_clause(s.c.max_age_rating),
            )
        ).all()
        pending: dict[str, BoundShelf] = {}
        for row in rows:
            entries = row.path_map if isinstance(row.path_map, list) else []
            base = next(
                (
                    entry["local"]
                    for entry in entries
                    if isinstance(entry, dict)
                    and entry.get("mode", "rw") == "rw"
                    and isinstance(entry.get("local"), str)
                ),
                None,
            )
            if base is None:
                continue
            try:
                folder = clean_absolute(f"{base}/{row.root_subpath}" if row.root_subpath else base)
                rest = within(folder, clean_absolute(local))
                root = clean_absolute(f"{remote}/{rest}") if rest else clean_absolute(remote)
            except PathMapError:
                continue
            if rest is not None:
                pending.setdefault(root, BoundShelf(None, root, row.max_age_rating))
        return list(pending.values())

    def record(
        self, target_id: str, candidate: BoundShelf | None, reading: Reading | None,
        *, failed: str | None = None, stamp: Stamp | None = None,
    ) -> KomgaAuditReport:  # fmt: skip
        """Write what a reading says. Everything is decided inside one transaction, on the shelves
        bound **then**, and a reading begun before the one the state keeps is ignored: it neither
        restores a proof nor lifts a pause."""
        stamp = (reading.stamp if reading else None) or stamp or new_stamp(self._clock)
        with write_transaction(self._engine) as connection:
            previous = state.load(connection, target_id)
            kept, mine = _key(previous.get("stamp")), _key(stamp)
            if kept is not None and (mine is None or mine < kept):
                return KomgaAuditReport(proved=False, reason=AUDIT_SUPERSEDED)
            report = self._write(connection, target_id, candidate, reading, failed, stamp, previous)
        if report.paused_shelves:
            self._events.emit(
                "target.child_exposure", target_id=target_id, shelves=report.paused_shelves,
                accounts=[], cause=report.reason or AUDIT_FAILED,
            )  # fmt: skip
        return report

    def _write(
        self, connection: Any, target_id: str, candidate: BoundShelf | None,
        reading: Reading | None, failed: str | None, stamp: Stamp, previous: dict[str, Any],
    ) -> KomgaAuditReport:  # fmt: skip
        bound = self._bound(connection, target_id)
        # the day's audit replays the shelves bound now, the proofs the state holds (a folder
        # proved the day before stays proved only through it) and the folders of the links that
        # wait (a family shelf is refused only until the next audit, never forever); a link
        # (`admit`) asks about its candidate alone.
        roots = {b.root for b in bound}
        kept = [k for k in self._kept(previous) if k.root not in roots] if candidate is None else []
        pending = (
            [
                p
                for p in self._pending(connection, target_id)
                if p.root not in roots and p.root not in {k.root for k in kept}
            ]
            if candidate is None
            else []
        )
        shelves = [*bound, *kept, *pending, *([candidate] if candidate else [])]
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
            # a finding withdraws the proof of its folder only (L22 counter-review): a pending
            # folder no library maps must not take the proof of another away
            bad = {f.folder for f in findings if f.folder}
            proved = [
                {"root": shelf.root, "ceiling": shelf.ceiling}
                for shelf in shelves
                if shelf.root and cleaned(shelf.root) not in bad
            ]
        folders = [shelf for shelf in shelves if shelf.root]
        if candidate is not None:  # the question is the shelf about to be linked
            proved_now = complete and None not in {f.shelf_id for f in findings} and bool(folders)
        else:
            proved_now = complete and not findings and bool(folders)
        report = KomgaAuditReport(
            proved=proved_now,
            reason=None if proved_now else reason,
            findings=findings,
            failed=failed is not None,
            error=failed,
            version=reading.version if reading else None,
            warnings=reading.advisories if reading else (),
            folders=len(folders),
        )
        for shelf_id in sorted(must_pause):
            if hold(connection, target_id, shelf_id):
                report.paused_shelves.append(shelf_id)
        if complete:  # only a complete audit lets go, of what it held (bound or not) and of an
            held = held_by(
                connection, target_id
            )  # unheld pause on a shelf that has only this server
            bound_ids = {b.shelf_id for b in bound if b.shelf_id}
            for shelf_id in sorted((held | bound_ids) - must_pause):
                alone = shelf_id in bound_ids and not other_targets_of(
                    connection, shelf_id, target_id
                )
                if release(connection, target_id, shelf_id, lift_unheld=alone):
                    report.resumed_shelves.append(shelf_id)
        libraries = reading.libraries if reading else ()
        state.save(connection, target_id, {
            "audited_at": stamp[0],
            "stamp": list(stamp),
            "reason": reason,
            "proved": proved,
            "findings": [
                {"code": f.code, "library": f.library, "folder": f.folder} for f in findings
            ],
            "libraries": [{"ref": lib.ref, "root": lib.root} for lib in libraries],
            "child_libraries": child_reach(reading.accounts, libraries) if complete and reading
            else {},
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
        return report
