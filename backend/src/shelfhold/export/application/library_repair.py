"""Repair of a damaged library file, at its path, under the intent journal (EF-BIB-012, EF-EXP-016).

The integrity check found the archive Kavita and KOReader read damaged; the reacquisition fetched
the Unit again. The fresh bytes are audited (archive guard, allowlist, the payload the content
records), then put **in the place of the damaged file** by the same three steps as a same-path
upgrade: staged in the host folder of the root (never in the tree a library scans, so no
duplicate chapter appears), swapped with the damaged file in one step, the damaged file moved to
the bin of its root (30 days). The replacement is one `upgrade` intent recorded before the disk is
touched; a SIGKILL between two steps is settled at boot by the same replay as any upgrade
(`IntentJournal._resolve_upgrade`, with `LibraryRepair.settle_hook` for the bookkeeping): the path
is never empty, never holds two files, and the damaged file is never lost.

The barrier of the export is consulted first, as before any write into a shelf
(`barrier_closed_by`):
an Edition a shelf below 18+ no longer admits (its age raised, withdrawn) or a destination that is
closed must not become readable again by a repair. Then nothing is replaced: the fresh bytes are
kept **outside the library** (`/data/integrity/held/`), the Unit stays `corrupt`, `status` says
`withdrawal_pending` (`integrity_blocked_by_age`) with the exit of the withdrawal, and the job
tries again each night (`retry_held`) until the barrier opens or the withdrawal is applied.

The registry then names the file that is there (`record_repaired_copy`) and `unit.upgraded` says,
for the Activity, that KOReader's position was reset (D21). The Unit is `ok` only once the path was
read again (`IntegrityChecker.repaired`); nothing here marks it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.events.bus import EventBus
from shelfhold.export.application.intents import IntentJournal, ManifestHook, OpenIntent
from shelfhold.export.application.snapshot import barrier_closed_by
from shelfhold.export.application.upgrade import UpgradeMark, place_under_intent
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.held_files import remove_held_file
from shelfhold.infrastructure.logging import escaped
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.infrastructure.volumes import write_data_file
from shelfhold.library.public import (
    RepairRefusedError,
    audit_repair_archive,
    damaged_library_copies,
    irreplaceability,
    record_repaired_copy,
)

__all__ = ["REPAIR_KIND", "LibraryRepair", "barrier_closed_for", "held_path"]

log = structlog.get_logger(__name__)

REPAIR_KIND = "library_repair"
EVENT = "unit.upgraded"
WorkHolder = Callable[[str], AbstractContextManager[Any]]


def held_path(data_dir: Path, unit_id: str) -> Path:
    """Where fresh bytes wait for the barrier to open: in /data, never in a library."""
    return data_dir / "integrity" / "held" / f"{unit_id}.cbz"


def barrier_closed_for(connection: Connection, unit_id: str) -> str | None:
    """Why a file of this Unit must not be written again into a shelf it is served from:
    `destination` or `age` (the verdict of `barrier_closed_by`), None when every barrier holds or
    nothing is served from a shelf."""
    edition = connection.execute(
        sa.select(catalog.unit.c.edition_id).where(catalog.unit.c.id == unit_id)
    ).scalar_one_or_none()
    if edition is None:
        return None
    record, coverage = export.export_record, export.unit_coverage
    destinations = connection.execute(
        sa.select(record.c.shelf_destination_id)
        .select_from(
            record.join(
                coverage,
                sa.and_(
                    coverage.c.carrier_type == "export_item",
                    coverage.c.carrier_id == record.c.export_item_id,
                ),
            )
        )
        .where(coverage.c.unit_id == unit_id, record.c.locator_type == "path")
        .distinct()
        .order_by(record.c.shelf_destination_id)
    ).scalars()
    verdicts = [barrier_closed_by(connection, str(d), edition) for d in destinations]
    if "age" in verdicts:
        return "age"
    return "destination" if "destination" in verdicts else None


class LibraryRepair:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        locate: Callable[[str], tuple[str, str]],
        clock: Clock,
        *,
        hold_work: WorkHolder,
        bus: EventBus | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._engine = engine
        self._fsops = fsops
        self._locate = locate
        self._clock = clock
        self._hold_work = hold_work
        self._bus = bus if bus is not None else EventBus(clock)
        self._journal = IntentJournal(engine, fsops, clock=clock)

    def __call__(self, unit_id: str, variant_id: str, staged: Path, sha256: str) -> bool:
        """Put the fresh bytes of `staged` in the place of every damaged library file of the Unit.
        True when at least one file was replaced; a refused archive changes nothing, and a closed
        barrier keeps the bytes outside the library for later."""
        outcome = self._apply(unit_id, variant_id, staged, sha256)
        if outcome == "blocked":
            self._hold(unit_id, staged)
        return outcome == "replaced"

    def retry_held(self, unit_id: str) -> bool:
        """The fresh bytes kept while a barrier was closed: tried again; they go once used or
        refused, and stay while the barrier is still closed."""
        if self._data_dir is None:
            return False
        kept = held_path(self._data_dir, unit_id)
        if not kept.is_file():
            return False
        with self._engine.connect() as connection:
            variant = connection.execute(
                sa.select(catalog.unit_variant.c.id)
                .where(catalog.unit_variant.c.unit_id == unit_id)
                .order_by(catalog.unit_variant.c.id)
                .limit(1)
            ).scalar_one_or_none()
        if variant is None:
            return False
        sha = "sha256:" + hashlib.sha256(kept.read_bytes()).hexdigest()
        outcome = self._apply(unit_id, str(variant), kept, sha)
        if outcome in ("replaced", "refused", "none"):
            remove_held_file(held_path(self._data_dir, unit_id))  # our own working data
        return outcome == "replaced"

    def _hold(self, unit_id: str, staged: Path) -> None:
        if self._data_dir is None:
            log.warning("integrity.repair_not_held", unit_id=unit_id)
            return
        rel = held_path(self._data_dir, unit_id).relative_to(self._data_dir).as_posix()
        write_data_file(self._data_dir, rel, staged.read_bytes())
        log.warning("integrity.blocked_by_age", unit_id=unit_id)

    def _apply(self, unit_id: str, variant_id: str, staged: Path, sha256: str) -> str:
        try:
            new_hash = audit_repair_archive(self._engine, variant_id, staged)
        except RepairRefusedError as error:
            log.warning("integrity.repair_refused", unit_id=unit_id, error=str(error)[:80])
            return "refused"
        if new_hash != sha256:  # what was received is not what was audited
            log.warning("integrity.repair_refused", unit_id=unit_id, error="received_hash_changed")
            return "refused"
        with self._engine.connect() as connection:
            if irreplaceability(connection, unit_id).irreplaceable:  # no source: never swapped
                log.warning("integrity.repair_refused", unit_id=unit_id, error="irreplaceable")
                return "refused"
            if not damaged_library_copies(connection, unit_id):
                return "none"
            work_id = connection.execute(
                sa.select(catalog.edition.c.work_id)
                .join(catalog.unit, catalog.unit.c.edition_id == catalog.edition.c.id)
                .where(catalog.unit.c.id == unit_id)
            ).scalar_one_or_none()
        done = 0
        with self._hold_work(work_id) if work_id else nullcontext():
            with self._engine.connect() as connection:  # read under the lock: no gap to change in
                if barrier_closed_for(connection, unit_id) is not None:
                    return "blocked"
                copies = damaged_library_copies(connection, unit_id)
            for copy in copies:
                done += self._swap(unit_id, copy.replica_id, copy.local, staged, new_hash)
        return "replaced" if done else "none"

    def _swap(self, unit_id: str, replica_id: str, target: str, staged: Path, new_hash: str) -> int:
        found = self._fsops.hash_and_identity(target)
        if found is None:  # the damaged file is gone: nothing sits at the path to be replaced
            log.warning("integrity.repair_no_target", unit_id=unit_id, path=escaped(target))
            return 0
        old_sha, old_identity, _dev = found
        if old_sha == new_hash:  # already the fresh bytes (a replay): only the registry is owed
            return 0
        mark = UpgradeMark(
            old_identity.ino, old_identity.size, old_identity.mtime_ns, old_sha256=old_sha,
            fields=(("kind", REPAIR_KIND), ("unit_id", unit_id), ("replica_id", replica_id)),
        )  # fmt: skip
        with staged.open("rb") as stream:
            intent = place_under_intent(
                self._journal, self._fsops, target, mark=mark, new_hash=new_hash, data=stream
            )
        with self._engine.begin() as connection:
            event = self._complete(connection, intent, mark.to_json())
            self._journal.mark_done(intent, within=connection)
        if event is not None:
            self._bus.deliver([event])
        log.info("integrity.repair_done", unit_id=unit_id)
        return 1

    def _complete(self, connection: Connection, intent_id: str, payload: dict[str, Any]) -> Any:
        """The registry names the file now at the path; the Activity says KOReader starts again."""
        row = connection.execute(
            sa.text("SELECT dst_path, expected_sha256 FROM fs_intent WHERE id = :i"),
            {"i": intent_id},
        ).one_or_none()
        if row is None:
            raise LookupError(f"repair intent {intent_id} is gone")
        observed = self._fsops.observe(str(row.dst_path))
        if observed is None:
            raise FileNotFoundError(str(row.dst_path))
        identity = observed[1]
        record_repaired_copy(
            connection, str(payload["replica_id"]), file_hash=str(row.expected_sha256),
            dev=self._fsops.device_of(str(row.dst_path)), ino=identity.ino, size=identity.size,
            mtime_ns=identity.mtime_ns, verified_at=self._clock.now(),
        )  # fmt: skip
        return self._bus.publish(
            connection, EVENT,
            {
                "unit_id": payload["unit_id"], "reason": "integrity_reacquire",
                "automatic": True, "koreader_reset": True,
            },
            visibility="ops",  # the Activity of the admin (D21)
        )  # fmt: skip

    def settle_hook(self) -> ManifestHook:
        """For `IntentJournal.resolve_open(on_done=...)` at boot: the registry owed by a repair
        the replay finished. Other operations are not ours."""

        def settle(connection: Connection, intent: OpenIntent) -> None:
            if intent.op == "upgrade" and intent.payload.get("kind") == REPAIR_KIND:
                self._complete(connection, intent.id, intent.payload)

        return settle
