"""Deleting a Unit at three levels, with the impact shown first (EF-BIB-021, EF-BIB-031).

1. **stop following**: the Unit stays, nothing is fetched for it again; no file moves;
2. **take out of the destinations**: the files served in the destinations go to the bin of their
   root; `/library` and `/store` keep theirs; refused for a file that is the only original there
   is (an adopted file) and for any reading that would leave no independent copy;
3. **delete the content**: every managed copy goes to the bin for 30 days and the Unit is no
   longer followed. An irreplaceable Unit (nothing upstream can give it back) needs its
   identifier typed.

The **preview** (`DeletionService.preview`) writes nothing, not even a lock, and its `version`
digests all it announces. The **application** is the same walk, redone under the locks: it does
only what a plan of the version the admin read says (`plan.stale` otherwise). Applying takes the
`work` lock of every Work concerned (the one an export and "Mettre à l'abri" hold, so that a
deletion and they never interleave) and one `path` per destination folder. Then, per Unit:

1. one transaction: an intent (`recycle`, naming its bin entry) per file, one `trash_item` per
   file holding the registry row and the export record as they were, the rows forgotten, the
   audit entry: all of it or nothing;
2. each file moves into the bin of its own root (never deleted: 30 days, restorable, with the
   same hash); a move that fails is replayed at once through its intent, then left to the next
   start;
3. Kavita is asked to scan the folders the files left, and **after that** the series folders that
   Shelfhold manages and that are now empty are removed (never a folder of an adopted
   destination, never one that still holds anything, `.shelfhold` included).

A file the registry does not know, a folder it did not create and a read-only root are never
touched. The expiry of the bin is the retention sweep's (`RecycleSweeper`).
"""

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.export.application.intents import BIN_LOCK, IntentJournal, open_intent_paths
from shelfhold.export.application.locks import ResourceLocks, path_resource, work_resource
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.physical_roots import is_reserved
from shelfhold.identity.public import AuditEntry, Principal, audit_insert
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, platform, reading, storage
from shelfhold.library.public import (
    BinnedUnitFile,
    DeletionLevel,
    DeletionPlan,
    FileImpact,
    UnitFile,
    UnitImpact,
    binned_for_unit,
    drop_replicas,
    forget_bin_rows,
    has_readable_copy,
    independent_copies,
    plan_version,
    record_binned_file,
    replica_snapshot,
    require_deletion_allowed,
    require_reserved_deletion,
    restore_replica,
    safe_to_clean,
    unit_files,
)

__all__ = [
    "DeletionBlockedError",
    "DeletionReport",
    "DeletionService",
    "DeletionUnknownError",
    "RestoreBlockedError",
    "RestoreReport",
]

log = structlog.get_logger(__name__)

LIVE_STATES: Final = ("written", "scanned", "resolved", "adopted")
LEVEL_TO_KIND: Final = {
    DeletionLevel.REMOVE_DESTINATIONS: "export_file",
    DeletionLevel.DELETE_CONTENT: "unit",
}

# why a Unit cannot be deleted at a level (codes, never a path or a title)
COLLATERAL: Final = "collateral"  # a file that also covers other Units
ADOPTED_ORIGINAL: Final = "adopted_original"  # the file is the only original there is
BUSY: Final = "open_intent"  # an operation is already moving that file


class DeletionUnknownError(LookupError):
    """A Unit that does not exist."""


class DeletionBlockedError(Exception):
    """The plan holds a Unit that may not be deleted at this level (nothing was changed)."""

    code = "deletion.blocked"

    def __init__(self, reasons: Mapping[str, tuple[str, ...]]) -> None:
        super().__init__(f"deletion blocked: {dict(reasons)}")
        self.reasons = dict(reasons)


class RestoreBlockedError(Exception):
    """A restore that cannot put every file back exactly as it was (nothing was moved). `files`:
    `(path in its root, reason)` for each file that cannot go back, so that the rest may be
    restored with `partial`."""

    code = "deletion.blocked"

    def __init__(self, reason: str, *, files: tuple[tuple[str, str], ...] = ()) -> None:
        super().__init__(f"restore blocked: {reason}")
        self.reason = reason
        self.files = files


@dataclass(frozen=True)
class DeletionReport:
    level: DeletionLevel
    deletion_id: str
    units: tuple[str, ...]
    moved: int  # files now in a bin
    pending: tuple[str, ...]  # files whose move waits for the next start (an intent is open)
    folders_removed: tuple[str, ...] = ()
    folders_kept: tuple[str, ...] = ()  # an empty folder left for a later pass (the scan failed)


@dataclass(frozen=True)
class RestoreReport:
    unit_id: str
    restored: int
    left_in_bin: tuple[
        tuple[str, str], ...
    ] = ()  # `(path, reason)` a partial restore did not put back
    pending: tuple[str, ...] = ()  # files whose move waits for the next start (an intent is open)


@dataclass
class _Item:
    """A file to move, with what it takes to do it and to put it back."""

    impact: FileImpact
    local: str
    unit_id: str
    replica_ids: list[str] = field(default_factory=list)
    destination_id: str | None = None
    previous_state: str | None = None


@dataclass
class _Scope:
    """Everything the walk found for one Unit (before it is judged)."""

    unit_id: str
    work_id: str
    items: list[_Item]
    untouched: list[str]
    exports: int
    progress: int
    export_item_ids: list[str]
    blocked: list[str]


class DeletionService:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        *,
        clock: Clock,
        locate: Callable[[str], tuple[str, str]],
        request_scan: Callable[[str], None] | None = None,
        refresh_index: Callable[[], object] | None = None,
        fresh_auth_s: int = 900,
        lock_wait: float = 5.0,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._clock = clock
        self._locate = locate
        self._scan = request_scan
        self._index = refresh_index
        self._fresh_auth_s = fresh_auth_s
        self._locks = ResourceLocks(engine, clock=clock)
        self._lock_wait = lock_wait

    # -- the preview: reads only --------------------------------------------------------------

    def preview(self, level: DeletionLevel, unit_ids: Sequence[str]) -> DeletionPlan:
        """What applying would do, to the file. Writes nothing: no row, no lock, no folder."""
        with self._engine.connect() as connection:
            return self._plan(connection, level, unit_ids)[0]

    def _plan(
        self, connection: Connection, level: DeletionLevel, unit_ids: Sequence[str]
    ) -> tuple[DeletionPlan, list[_Scope], list[str]]:
        ordered = sorted(set(unit_ids))
        if not ordered:
            raise DeletionUnknownError("no Unit named")
        known = set(
            connection.execute(
                sa.select(catalog.unit.c.id).where(catalog.unit.c.id.in_(ordered))
            ).scalars()
        )
        if missing := [u for u in ordered if u not in known]:
            raise DeletionUnknownError(f"unknown Unit: {missing[0]}")
        _allowed, kept = safe_to_clean(connection, ordered)  # the irreplaceable ones, flagged
        busy = open_intent_paths(connection)
        scopes = [self._scope(connection, level, u, busy) for u in ordered]
        collateral = self._collateral(connection, scopes, ordered)
        impacts: list[UnitImpact] = []
        for scope in scopes:
            moved = [r for item in scope.items for r in item.replica_ids]
            before = independent_copies(connection, scope.unit_id)
            after = independent_copies(connection, scope.unit_id, without=moved)
            blocked = list(scope.blocked)
            if scope.unit_id in collateral:
                blocked.append(COLLATERAL)
            irreplaceable = scope.unit_id in kept
            # the last READABLE copy of an irreplaceable Unit, wherever it sits: the destination
            # rows are never "independent", so the count above cannot see it (EF-BIB-031)
            takes_last_readable = (
                level is DeletionLevel.REMOVE_DESTINATIONS
                and irreplaceable
                and bool(scope.items)
                and not has_readable_copy(connection, scope.unit_id, without=moved)
            )
            impacts.append(
                UnitImpact(
                    scope.unit_id,
                    irreplaceable,
                    before,
                    after,
                    tuple(i.impact for i in scope.items) if level.moves_files else (),
                    tuple(sorted(scope.untouched)),
                    tuple(sorted(set(blocked))),
                    scope.exports,
                    scope.progress,
                    needs_confirmation=(level is DeletionLevel.DELETE_CONTENT and irreplaceable)
                    or takes_last_readable,
                )
            )
        folders = self._empty_folders(connection, scopes) if level.moves_files else []
        shown = tuple(f"{d}/{rel}" for d, rel in folders)
        plan = DeletionPlan(level, tuple(impacts), shown, plan_version(level, impacts, shown))
        return plan, scopes, [f"{d}/{rel}" for d, rel in folders]

    def _scope(
        self, connection: Connection, level: DeletionLevel, unit_id: str, busy: frozenset[str]
    ) -> _Scope:
        edition_work = connection.execute(
            sa.select(catalog.edition.c.work_id)
            .select_from(
                catalog.unit.join(
                    catalog.edition, catalog.edition.c.id == catalog.unit.c.edition_id
                )
            )
            .where(catalog.unit.c.id == unit_id)
        ).scalar_one()
        coverage = export.unit_coverage
        items = sorted(
            set(
                connection.execute(
                    sa.select(coverage.c.carrier_id).where(
                        coverage.c.carrier_type == "export_item", coverage.c.unit_id == unit_id
                    )
                ).scalars()
            )
        )
        contents = list(
            connection.execute(
                sa.select(storage.content.c.id).where(storage.content.c.unit_id == unit_id)
            ).scalars()
        )
        scope = _Scope(unit_id, str(edition_work), [], [], 0, 0, items, [])
        scope.progress = int(
            connection.execute(
                sa.select(sa.func.count())
                .select_from(reading.reading_position)
                .where(
                    sa.or_(
                        sa.and_(
                            reading.reading_position.c.carrier_type == "export_item",
                            reading.reading_position.c.carrier_id.in_(items),
                        ),
                        sa.and_(
                            reading.reading_position.c.carrier_type == "content",
                            reading.reading_position.c.carrier_id.in_(contents),
                        ),
                    )
                )
            ).scalar_one()
        )
        if not level.moves_files:
            return scope
        files = unit_files(connection, [unit_id], items)
        destination_rows = {
            (f.export_item_id, f.root_id, f.rel_path): f
            for f in files
            if f.location == "destination"
        }
        for f in files:
            if f.location in ("adopted_ro",) or f.root_mode != "rw":
                scope.untouched.append("read_only_root")
        if level is DeletionLevel.DELETE_CONTENT:
            for f in files:
                if (
                    f.location in ("library", "store")
                    and f.root_mode == "rw"
                    and f.unit_id == unit_id
                ):
                    self._add_file(scope, f, busy)
        record, dest = export.export_record, export.shelf_destination
        records = connection.execute(
            sa.select(record, dest.c.adopted)
            .select_from(record.join(dest, dest.c.id == record.c.shelf_destination_id))
            .where(
                record.c.export_item_id.in_(items),
                record.c.state.in_(LIVE_STATES),
                record.c.rel_path.is_not(None),
            )
            .order_by(record.c.shelf_destination_id, record.c.rel_path)
        ).all()
        for r in records:
            ctx = load_context(connection, r.shelf_destination_id)
            if ctx.root_local is None:
                continue
            local = ctx.local(r.rel_path)
            if self._fsops.is_read_only(local):
                scope.untouched.append("read_only_root")
                continue
            observed = self._fsops.observe(local)
            if observed is None or observed[0] != "file":
                continue  # not on disk: nothing to move, the record is the reconcile's business
            if r.state == "adopted" and level is DeletionLevel.REMOVE_DESTINATIONS:
                scope.blocked.append(ADOPTED_ORIGINAL)  # no copy of Shelfhold's own: level 3 only
                continue
            root_id, rel = self._locate(local)
            replica = destination_rows.get((r.export_item_id, root_id, rel))
            item = _Item(
                FileImpact(
                    "destination", root_id, rel, observed[1].size, r.file_hash,
                    r.export_item_id, r.shelf_destination_id,
                ),
                local, unit_id, [replica.replica_id] if replica else [],
                r.shelf_destination_id, r.state,
            )  # fmt: skip
            if local in busy:
                scope.blocked.append(BUSY)
            scope.items.append(item)
            scope.exports += 1
        return scope

    def _add_file(self, scope: _Scope, f: UnitFile, busy: frozenset[str]) -> None:
        local = f"{f.root_path.rstrip('/')}/{f.rel_path}"
        observed = self._fsops.observe(local)
        if observed is None or observed[0] != "file":
            return  # the registry row without a file: the verification pass settles it
        if local in busy:
            scope.blocked.append(BUSY)
        scope.items.append(
            _Item(
                FileImpact(f.location, f.root_id, f.rel_path, observed[1].size, f.file_hash),
                local, scope.unit_id, [f.replica_id],
            )
        )  # fmt: skip

    @staticmethod
    def _collateral(
        connection: Connection, scopes: Sequence[_Scope], selected: Sequence[str]
    ) -> set[str]:
        """The Units whose files also cover Units that are not in the selection."""
        coverage = export.unit_coverage
        found: set[str] = set()
        for scope in scopes:
            if not scope.export_item_ids:
                continue
            others = connection.execute(
                sa.select(sa.func.count(sa.distinct(coverage.c.unit_id))).where(
                    coverage.c.carrier_type == "export_item",
                    coverage.c.carrier_id.in_(scope.export_item_ids),
                    coverage.c.unit_id.not_in(list(selected)),
                )
            ).scalar_one()
            if others:
                found.add(scope.unit_id)
        return found

    def _empty_folders(
        self, connection: Connection, scopes: Sequence[_Scope]
    ) -> list[tuple[str, str]]:
        """`(destination, folder)` the application will try to remove after the scan: folders of a
        destination Shelfhold wrote (never an adopted one) that hold nothing once the files
        announced are gone, from the deepest to the series folder. Read from the disk now."""
        leaving: dict[str, set[str]] = {}  # destination -> local paths that go
        for scope in scopes:
            for item in scope.items:
                if item.destination_id is not None:
                    leaving.setdefault(item.destination_id, set()).add(item.local)
        folders: list[tuple[str, str]] = []
        for destination_id, going in sorted(leaving.items()):
            adopted = connection.execute(
                sa.select(export.shelf_destination.c.adopted).where(
                    export.shelf_destination.c.id == destination_id
                )
            ).scalar_one()
            if adopted:
                continue  # a folder somebody else made is never ours to remove
            ctx = load_context(connection, destination_id)
            if ctx.root_local is None:
                continue
            gone = set(going)
            candidates: set[str] = set()
            for local in going:
                rel = local.removeprefix(f"{ctx.root_local}/")
                parts = rel.split("/")[:-1]
                candidates.update("/".join(parts[:n]) for n in range(1, len(parts) + 1))
            for rel in sorted(candidates, key=lambda r: (-r.count("/"), r)):
                folder = f"{ctx.root_local}/{rel}"
                if self._is_empty_after(folder, gone):
                    folders.append((destination_id, rel))
                    gone.add(folder)
        return folders

    def _is_empty_after(self, folder: str, gone: set[str]) -> bool:
        try:
            entries = self._fsops.list_dir(folder)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return all(f"{folder}/{name}" in gone for name in entries)

    # -- the application ----------------------------------------------------------------------

    def apply(
        self,
        level: DeletionLevel,
        unit_ids: Sequence[str],
        *,
        if_match: str,
        typed: Mapping[str, str] | None,
        principal: Principal,
        now: datetime,
    ) -> DeletionReport:
        """Every check comes before the first write: the reserved rule, the plan as it stands
        under the locks (its version, its blocks), the word typed for each irreplaceable Unit."""
        require_reserved_deletion(principal=principal, now=now, fresh_auth_s=self._fresh_auth_s)
        first, scopes, _ = self._read(level, unit_ids)
        resources = self._resources(scopes)
        with self._locks.hold_resources(new_ulid(), resources, timeout=self._lock_wait):
            plan, scopes, folders = self._read(level, unit_ids)
            if plan.version != if_match:
                raise StalePlanError("the plan moved since it was read: read it again")
            if plan.blocked:
                raise DeletionBlockedError({u.unit_id: u.blocked for u in plan.units if u.blocked})
            self._allow(level, plan, principal, now, typed or {})
            if first.version != plan.version:  # a file arrived while the locks were taken
                raise StalePlanError("a file arrived while the locks were taken: read again")
            return self._apply_locked(level, plan, scopes, folders, principal, now)

    def _read(
        self, level: DeletionLevel, unit_ids: Sequence[str]
    ) -> tuple[DeletionPlan, list[_Scope], list[str]]:
        with self._engine.connect() as connection:
            return self._plan(connection, level, unit_ids)

    def _resources(self, scopes: Sequence[_Scope]) -> list[str]:
        works = sorted({s.work_id for s in scopes})
        folders: set[str] = set()
        with self._engine.connect() as connection:
            for scope in scopes:
                for item in scope.items:
                    if item.destination_id is None:
                        continue
                    ctx = load_context(connection, item.destination_id)
                    rel = item.local.removeprefix(f"{ctx.root_local}/")
                    folders.add(f"{item.destination_id}/{rel.split('/', 1)[0]}")
        return [*(work_resource(w) for w in works), *(path_resource(f) for f in sorted(folders))]

    def _allow(
        self,
        level: DeletionLevel,
        plan: DeletionPlan,
        principal: Principal,
        now: datetime,
        typed: Mapping[str, str],
    ) -> None:
        if level is DeletionLevel.UNFOLLOW:
            return
        with self._engine.connect() as connection:
            for unit in plan.units:  # irreplaceability is read again, now, for each Unit
                if level is DeletionLevel.REMOVE_DESTINATIONS and not unit.needs_confirmation:
                    continue
                require_deletion_allowed(
                    connection, unit.unit_id, principal=principal, now=now,
                    typed=typed.get(unit.unit_id), fresh_auth_s=self._fresh_auth_s,
                )  # fmt: skip

    def _apply_locked(
        self,
        level: DeletionLevel,
        plan: DeletionPlan,
        scopes: Sequence[_Scope],
        folders: Sequence[str],
        principal: Principal,
        now: datetime,
    ) -> DeletionReport:
        deletion_id = new_ulid()
        journal = IntentJournal(self._engine, self._fsops, clock=self._clock)
        moved = 0
        pending: list[str] = []
        for scope in scopes:
            for item in scope.items:
                self._fsops.require_delete_allowed(item.local)  # the sentinel's word, before all
        for scope in scopes:
            deeds = self._commit(level, scope, deletion_id, journal, principal, now)
            for local, intent_id, entry in deeds:
                if self._move(journal, local, intent_id, entry):
                    moved += 1
                else:
                    pending.append(local)
        if level is DeletionLevel.DELETE_CONTENT and moved:
            self._refresh_index()  # /store/INDEX.jsonl stops citing the copies now in the bin
        removed: tuple[str, ...] = ()
        kept: tuple[str, ...] = ()
        if folders and not pending:
            removed, kept = self._after_scan(plan, scopes, journal)
        log.info(
            "deletion.applied", level=level.value, units=len(scopes), files=moved,
            pending=len(pending), folders=len(removed),
        )  # fmt: skip
        return DeletionReport(
            level, deletion_id, tuple(s.unit_id for s in scopes), moved, tuple(pending),
            removed, kept,
        )  # fmt: skip

    def _commit(
        self,
        level: DeletionLevel,
        scope: _Scope,
        deletion_id: str,
        journal: IntentJournal,
        principal: Principal,
        now: datetime,
    ) -> list[tuple[str, str, str]]:
        deeds: list[tuple[str, str, str]] = []
        record = export.export_record
        with write_transaction(self._engine) as connection:
            follow_set = False
            if level is DeletionLevel.DELETE_CONTENT or level is DeletionLevel.UNFOLLOW:
                follow_set = self._suppress(connection, scope.unit_id, now)
            for item in scope.items:
                observed = self._fsops.observe(item.local)
                if observed is None or observed[0] != "file":
                    continue  # it left meanwhile (the locks say nobody of ours moved it)
                digest = self._fsops.sha256_of(item.local)
                entry = uuid.uuid4().hex
                intent = journal.record(
                    op="recycle", src=item.local, dst=entry, within=connection,
                    expected_sha256=digest, expected_file_id=observed[1],
                )  # fmt: skip
                snapshot: dict[str, Any] = {
                    "unit_id": scope.unit_id,
                    "place": item.impact.place,
                    "root_id": item.impact.root_id,
                    "rel_path": item.impact.rel_path,
                    "sha256": digest,
                    "export_item_id": item.impact.export_item_id,
                    "destination_id": item.destination_id,
                    "record_state": item.previous_state,
                    "replicas": [
                        s for r in item.replica_ids if (s := replica_snapshot(connection, r))
                    ],
                    "follow_set": follow_set,
                }
                kind = LEVEL_TO_KIND[level]
                owner = item.impact.export_item_id if kind == "export_file" else scope.unit_id
                record_binned_file(
                    connection, kind=kind, ref=owner or scope.unit_id,
                    level=level.value, deletion_id=deletion_id, root_id=item.impact.root_id,
                    path=f"{entry}/{item.impact.rel_path}", size=observed[1].size,
                    snapshot=snapshot, now=now,
                )  # fmt: skip
                drop_replicas(connection, item.replica_ids)
                if item.destination_id is not None:
                    connection.execute(
                        sa.update(record)
                        .where(
                            record.c.shelf_destination_id == item.destination_id,
                            record.c.export_item_id == item.impact.export_item_id,
                        )
                        .values(state="recycled")
                    )
                deeds.append((item.local, intent, entry))
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=principal.actor_id, action="unit.delete", at=now,
                        target=f"unit:{scope.unit_id};{level.value};{deletion_id}",
                        session_id=principal.session_id, auth_time=principal.auth_time,
                    )
                )
            )  # fmt: skip
        return deeds

    @staticmethod
    def _suppress(connection: Connection, unit_id: str, now: datetime) -> bool:
        unit = catalog.unit
        done = connection.execute(
            sa.update(unit)
            .where(unit.c.id == unit_id, sa.not_(unit.c.follow_suppressed))
            .values(follow_suppressed=True, follow_suppressed_at=now)
        )
        return bool(done.rowcount)

    def _move(self, journal: IntentJournal, local: str, intent_id: str, entry: str) -> bool:
        """The file leaves for the bin entry its row names; a failure is replayed at once through
        the intent (the resolver of the start), and if it still fails the intent stays open."""
        try:
            journal.mark_applying(intent_id)
            self._fsops.recycle(local, entry=entry)
            journal.mark_done(intent_id)
            return True
        except Exception:
            log.warning("deletion.move_failed", intent_id=intent_id)
        try:
            report = journal.resolve_one(intent_id)
        except Exception:
            log.warning("deletion.move_pending", intent_id=intent_id)
            return False
        return report is None or intent_id in {*report.completed, *report.replayed}

    # -- after the files left: the scan, then the empty folders ---------------------------------

    def _after_scan(
        self, plan: DeletionPlan, scopes: Sequence[_Scope], journal: IntentJournal
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        wanted = [tuple(f.split("/", 1)) for f in plan.folders]
        contexts: dict[str, DestinationContext] = {}
        with self._engine.connect() as connection:
            for destination_id, _rel in wanted:
                if destination_id not in contexts:
                    contexts[destination_id] = load_context(connection, destination_id)
        if self._scan is not None:
            seen: set[str] = set()
            for destination_id, rel in wanted:
                top = rel.split("/", 1)[0]
                ctx = contexts[destination_id]
                local = ctx.local(top)
                if local in seen:
                    continue
                seen.add(local)
                try:
                    self._scan(ctx.remote_folder(local))
                except Exception:
                    log.warning("deletion.scan_failed")
                    return (), tuple(plan.folders)  # the folders stay: a later pass takes them
        return self.remove_empty_folders(
            [(d, r) for d, r in wanted], contexts=contexts, journal=journal
        )

    def remove_empty_folders(
        self,
        folders: Sequence[tuple[str, str]],
        *,
        contexts: Mapping[str, DestinationContext] | None = None,
        journal: IntentJournal | None = None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Remove the folders of destinations Shelfhold wrote that are empty **now** (an `rmdir`
        intent each, and `rmdir` itself refuses a folder that holds anything). Never the root of
        a destination, a reserved folder or a folder of an adopted destination. Returns
        `(removed, kept)` as `<destination>/<folder>`."""
        journal = journal or IntentJournal(self._engine, self._fsops, clock=self._clock)
        removed: list[str] = []
        kept: list[str] = []
        for destination_id, rel in sorted(folders, key=lambda f: (-f[1].count("/"), f)):
            label = f"{destination_id}/{rel}"
            with self._engine.connect() as connection:
                adopted = connection.execute(
                    sa.select(export.shelf_destination.c.adopted).where(
                        export.shelf_destination.c.id == destination_id
                    )
                ).scalar_one_or_none()
                ctx = (contexts or {}).get(destination_id) or (
                    load_context(connection, destination_id) if adopted is not None else None
                )
            if adopted or ctx is None or ctx.root_local is None or not rel or is_reserved(rel):
                kept.append(label)
                continue
            folder = ctx.local(rel)
            try:
                entries = self._fsops.list_dir(folder)
            except (FileNotFoundError, NotADirectoryError):
                continue
            if entries:
                kept.append(label)  # something is in it that is not ours to remove
                continue
            try:
                self._fsops.require_delete_allowed(folder)
                intent = journal.record(op="rmdir", src=folder)
                journal.mark_applying(intent)
                self._fsops.rmdir(folder)
                journal.mark_done(intent)
                removed.append(label)
            except (FsopsError, OSError):
                kept.append(label)
        return tuple(removed), tuple(kept)

    # -- level 1 --------------------------------------------------------------------------------

    def apply_unfollow(
        self, unit_ids: Sequence[str], *, if_match: str, principal: Principal, now: datetime
    ) -> DeletionReport:
        return self.apply(
            DeletionLevel.UNFOLLOW, unit_ids, if_match=if_match, typed=None,
            principal=principal, now=now,
        )  # fmt: skip

    # -- the restore ----------------------------------------------------------------------------

    def restore(
        self,
        unit_id: str,
        *,
        principal: Principal,
        now: datetime,
        deletion_id: str | None = None,
        partial: bool = False,
    ) -> RestoreReport:
        """Put back the files of ONE deletion of the Unit (the latest, or the one named), as they
        were: the same path, the same bytes (checked), the same registry rows and records. A file
        that cannot go back (gone, changed, its path taken, its root away) blocks only itself:
        by default the restore then refuses as a whole and says which; with `partial` the others
        go back and the report lists the ones left in the bin."""
        require_reserved_deletion(principal=principal, now=now, fresh_auth_s=self._fresh_auth_s)
        with self._engine.connect() as connection:
            found = binned_for_unit(connection, unit_id, deletion_id=deletion_id)
            work = connection.execute(
                sa.select(catalog.edition.c.work_id)
                .select_from(
                    catalog.unit.join(
                        catalog.edition, catalog.edition.c.id == catalog.unit.c.edition_id
                    )
                )
                .where(catalog.unit.c.id == unit_id)
            ).scalar_one_or_none()
        if work is None:
            raise DeletionUnknownError(f"unknown Unit: {unit_id}")
        if not found:
            raise RestoreBlockedError("nothing_to_restore")
        with (
            self._locks.hold_resources(
                new_ulid(), [work_resource(str(work))], timeout=self._lock_wait
            ),
            BIN_LOCK,
        ):  # no emptying of the bin between the check of the files and the moves
            return self._restore_locked(unit_id, found, principal, now, partial)

    def _restore_locked(
        self,
        unit_id: str,
        found: Sequence[BinnedUnitFile],
        principal: Principal,
        now: datetime,
        partial: bool,
    ) -> RestoreReport:
        good: list[tuple[BinnedUnitFile, str]] = []
        left: list[tuple[str, str]] = []
        with self._engine.connect() as connection:
            for binned in found:
                reason = "root_gone"
                local = None
                try:
                    local = self._target(connection, binned)
                    self._check_restorable(binned, local)
                except RestoreBlockedError as error:
                    reason = error.reason
                else:
                    good.append((binned, local))
                    continue
                left.append((str(binned.snapshot.get("rel_path", "")), reason))
        if left and (not partial or not good):
            raise RestoreBlockedError(left[0][1], files=tuple(left))
        journal = IntentJournal(self._engine, self._fsops, clock=self._clock)
        intents: list[tuple[str, str, str]] = []
        # the rows, the bin index and the intents of every move: one transaction. A crash after
        # it leaves the intents, which the start replays (the file goes back into its place); a
        # failure before it leaves the files where they are
        with write_transaction(self._engine) as connection:
            follow = any(b.snapshot.get("follow_set") for b, _ in good)
            for binned, local in good:
                snap = binned.snapshot
                for row in snap.get("replicas", []):
                    restore_replica(connection, row)
                if snap.get("destination_id") and snap.get("record_state"):
                    connection.execute(
                        sa.update(export.export_record)
                        .where(
                            export.export_record.c.shelf_destination_id == snap["destination_id"],
                            export.export_record.c.export_item_id == snap["export_item_id"],
                        )
                        .values(state=snap["record_state"])
                    )
                intent = journal.record(
                    op="unbin", src=local, dst=binned.path, expected_sha256=snap["sha256"],
                    within=connection,
                )  # fmt: skip
                intents.append((intent, local, binned.path))
            if follow:
                connection.execute(
                    sa.update(catalog.unit)
                    .where(catalog.unit.c.id == unit_id)
                    .values(follow_suppressed=False, follow_suppressed_at=None)
                )
            forget_bin_rows(connection, [b.trash_id for b, _ in good])
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=principal.actor_id, action="unit.restore", at=now,
                        target=f"unit:{unit_id};{found[0].deletion_id}",
                        session_id=principal.session_id, auth_time=principal.auth_time,
                    )
                )
            )  # fmt: skip
        pending: list[str] = []
        for intent, local, path in intents:
            if not self._unbin(journal, intent, local, path):
                pending.append(local)
        self._refresh_index()
        log.info("deletion.restored", files=len(good), left=len(left), pending=len(pending))
        return RestoreReport(unit_id, len(good), tuple(left), tuple(pending))

    def _unbin(self, journal: IntentJournal, intent_id: str, local: str, path: str) -> bool:
        """The file goes back for the entry its row names; a failure is replayed at once through
        the intent, and if it still fails the intent stays open (the next start finishes)."""
        try:
            journal.mark_applying(intent_id)
            self._fsops.restore_from_bin(local, path)
            journal.mark_done(intent_id)
            return True
        except Exception:
            log.warning("deletion.unbin_failed", intent_id=intent_id)
        try:
            report = journal.resolve_one(intent_id)
        except Exception:
            return False
        return report is None or intent_id in {*report.completed, *report.replayed}

    def _refresh_index(self) -> None:
        if self._index is not None:
            try:
                self._index()  # /store/INDEX.jsonl follows what /store holds now
            except Exception:
                log.warning("deletion.index_not_rewritten")  # the window's own pass draws it

    def _target(self, connection: Connection, binned: BinnedUnitFile) -> str:
        entry = connection.execute(
            sa.select(platform.physical_root.c.path_entry).where(
                platform.physical_root.c.root_id == binned.root_id
            )
        ).scalar_one_or_none()
        if entry is None:
            raise RestoreBlockedError("root_gone")
        return f"{str(entry).rstrip('/')}/{binned.snapshot['rel_path']}"

    def _check_restorable(self, binned: BinnedUnitFile, local: str) -> None:
        expected = binned.snapshot.get("sha256")
        try:
            with self._fsops.open_bin_entry(local, binned.path) as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
        except (FileNotFoundError, FsopsError, OSError):
            raise RestoreBlockedError("bin_entry_gone") from None
        if f"sha256:{digest.hexdigest()}" != expected:
            raise RestoreBlockedError("bytes_changed")
        if self._fsops.observe(local) is not None:
            raise RestoreBlockedError("path_taken")
        self._fsops.require_delete_allowed(local)  # the sentinel's word: the root accepts writes
