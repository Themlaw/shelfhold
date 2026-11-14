"""The removal plan of a family shelf (EF-BIB-037, EF-BIB-055): what leaves, shown before it goes.

An Edition filed on a shelf below 18+ that the admission now refuses (a Work rated higher, an
uncensored Edition rated 18+, a suggested age above its rating, a shelf lowered) must leave the
library the children can read. The plan lists those Editions and the files they hold; nothing moves
until an admin applies it, by a fresh session or the host CLI (`shelf.withdraw`, never a key), with
the version of the plan they read (`plan.stale`, 412, when anything moved since).

Applying takes the `work` lock of every Edition concerned, the one an export holds from its last
look at the barrier to its write: a withdrawal and an export of the same Work never interleave,
so no file can land after the plan was read. Then, per Edition and in this order:

1. one transaction: an intent (`recycle`) per file with the hash of the file it is for, the records
   `recycled`, the Edition unshelved ("À classer"), the audit entry: all of it or nothing;
2. the file moves into the bin of its root (`.shelfhold/recycle`, never deleted, restorable); a
   move that fails is replayed at once, then left to the next start;
3. Kavita is asked to scan the folder the file left, which is how it forgets the series.

A crash after 1 leaves the intent: the next start finishes the move of that file and no other.
"""

import hashlib
import json
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import RatingService
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import LockBusyError, ResourceLocks, work_resource
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.domain.bindings import is_family
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    AuditEntry,
    Principal,
    audit_insert,
    authorize_reserved,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import read_transaction, write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, storage
from shelfhold.infrastructure.stable_hash import FileChangedError, HashedFiles, retry_when_changed
from shelfhold.library.public import has_independent_store_copy, record_replica

__all__ = [
    "NotChildLibraryError",
    "WithdrawalBusyError",
    "WithdrawalItem",
    "WithdrawalPlan",
    "WithdrawalRefusedError",
    "WithdrawalReport",
    "WithdrawalRootReadOnlyError",
    "WithdrawalService",
    "WithdrawnFile",
    "adopted_unrated_plan",
    "build_plan",
    "plan_version",
]

log = structlog.get_logger(__name__)

LIVE_STATES = ("written", "scanned", "resolved", "adopted")  # a file may be in the shelf


class WithdrawalBusyError(Exception):
    """A job is exporting one of these Works: try again in a moment (nothing was changed)."""

    code = "lock.busy"


class NotChildLibraryError(Exception):
    """The destination is not a library the children can read: there is nothing to withdraw."""


class WithdrawalRootReadOnlyError(Exception):
    """The files sit under a read-only adoption root (`ADOPT_RO`): Shelfhold never moves them."""


class WithdrawalRefusedError(Exception):
    """The caller may not apply a removal plan (the verdict of `authorize_reserved`)."""


@dataclass(frozen=True)
class WithdrawnFile:
    destination_id: str
    export_item_id: str
    rel_path: str
    sha256: str = ""  # adopted files: the bytes the plan was read for


@dataclass(frozen=True)
class WithdrawalItem:
    edition_id: str
    shelf_id: str
    reason: str  # an `Admission` value
    files: tuple[WithdrawnFile, ...]


@dataclass(frozen=True)
class WithdrawalPlan:
    items: tuple[WithdrawalItem, ...]
    version: str  # what the admin read; compared again at the application


@dataclass(frozen=True)
class WithdrawalReport:
    withdrawn: tuple[str, ...]  # Editions unshelved
    files: int  # files taken out of the library
    pending: tuple[str, ...]  # files whose move waits for the next start (an intent is open)


def build_plan(
    connection: Connection, rating: RatingService, shelf_id: str | None = None
) -> WithdrawalPlan:
    """Every Edition whose files are in the library of a shelf below 18+ and must not be: the ones
    the admission refuses now, and the ones filed nowhere else (unshelved, or on another shelf)
    that still hold live records there, so that no file is left behind."""
    ceilings = dict(
        connection.execute(sa.select(export.shelf.c.id, export.shelf.c.max_age_rating)).all()
    )
    found: dict[tuple[str, str], str] = {
        (w.edition_id, w.shelf_id): w.reason.value
        for w in rating.withdrawals(connection, shelf_id=shelf_id)
        if is_family(ceilings[w.shelf_id])  # an adult shelf keeps what it holds
    }
    record, item = export.export_record, export.export_item
    destination, edition = export.shelf_destination, catalog.edition
    stray = connection.execute(
        sa.select(item.c.edition_id, destination.c.shelf_id)
        .select_from(
            record.join(item, item.c.id == record.c.export_item_id)
            .join(destination, destination.c.id == record.c.shelf_destination_id)
            .join(edition, edition.c.id == item.c.edition_id)
        )
        .where(
            record.c.state.in_(LIVE_STATES),
            record.c.rel_path.is_not(None),
            edition.c.shelf_id.is_distinct_from(destination.c.shelf_id),
        )
        .distinct()
    )
    for r in stray:
        if is_family(ceilings[r.shelf_id]) and shelf_id in (None, r.shelf_id):
            found.setdefault((r.edition_id, r.shelf_id), "unshelved")
    items = [
        WithdrawalItem(edition_id, shelf, reason, _files(connection, edition_id, shelf))
        for (edition_id, shelf), reason in sorted(found.items())
    ]
    material = json.dumps([asdict(i) for i in items], sort_keys=True, separators=(",", ":"))
    return WithdrawalPlan(tuple(items), hashlib.sha256(material.encode()).digest()[:16].hex())


def plan_version(files: Iterable[tuple[str, str, str, str]]) -> str:
    """The version of a plan of the unrated adopted files, from `(destination_id, export_item_id,
    rel_path, sha256)`: any rating, any new or missing file, any other item and any other bytes
    change it."""
    material = json.dumps(sorted(set(files)), separators=(",", ":"))
    return hashlib.sha256(material.encode()).digest()[:16].hex()


def adopted_unrated_plan(connection: Connection, destination_id: str) -> WithdrawalPlan:
    """Owner decision 2026-11-12: the files the adoption of a child library left readable for the
    Works nobody rated (the list of `shelf.follow_suspended`), on any shelf: an 18+ shelf that
    restricted accounts read is not a family shelf for `build_plan`, but its unrated files are
    just as readable. Rating a Work lifts its suspension and takes it out of the plan."""
    destination = export.shelf_destination
    shelf_id = connection.execute(
        sa.select(destination.c.shelf_id).where(destination.c.id == destination_id)
    ).scalar_one_or_none()
    if shelf_id is None:
        raise LookupError(f"no such destination: {destination_id}")
    suspended, ceiling = connection.execute(
        sa.select(export.shelf.c.follow_suspended, export.shelf.c.max_age_rating).where(
            export.shelf.c.id == shelf_id
        )
    ).one()
    if not (is_family(ceiling) or suspended):
        raise NotChildLibraryError(f"{destination_id} is not a library the children can read")
    works = [str(w) for w in (suspended or [])]
    editions = (
        connection.execute(
            sa.select(catalog.edition.c.id)
            .where(catalog.edition.c.work_id.in_(works), catalog.edition.c.shelf_id == shelf_id)
            .order_by(catalog.edition.c.id)
        ).scalars()
        if works
        else ()
    )
    items = [
        WithdrawalItem(edition_id, shelf_id, "adopted_unrated", files)
        for edition_id in editions
        if (
            files := tuple(
                _with_bytes(connection, f)
                for f in _files(connection, edition_id, shelf_id)
                if f.destination_id == destination_id
            )
        )
    ]
    version = plan_version(
        (f.destination_id, f.export_item_id, f.rel_path, f.sha256) for i in items for f in i.files
    )
    return WithdrawalPlan(tuple(items), version)


def _with_bytes(connection: Connection, file: WithdrawnFile) -> WithdrawnFile:
    digest = connection.execute(
        sa.select(storage.content.c.original_sha256)
        .select_from(
            export.export_item.join(
                storage.content, storage.content.c.id == export.export_item.c.content_id
            )
        )
        .where(export.export_item.c.id == file.export_item_id)
    ).scalar_one_or_none()
    return WithdrawnFile(file.destination_id, file.export_item_id, file.rel_path, digest or "")


def _files(connection: Connection, edition_id: str, shelf_id: str) -> tuple[WithdrawnFile, ...]:
    record, item = export.export_record, export.export_item
    destination = export.shelf_destination
    rows = connection.execute(
        sa.select(record.c.shelf_destination_id, record.c.export_item_id, record.c.rel_path)
        .select_from(
            record.join(item, item.c.id == record.c.export_item_id).join(
                destination, destination.c.id == record.c.shelf_destination_id
            )
        )
        .where(
            item.c.edition_id == edition_id,
            destination.c.shelf_id == shelf_id,
            record.c.state.in_(LIVE_STATES),
            record.c.rel_path.is_not(None),
        )
        .order_by(record.c.rel_path)
    )
    return tuple(WithdrawnFile(r.shelf_destination_id, r.export_item_id, r.rel_path) for r in rows)


class WithdrawalService:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        *,
        clock: Clock,
        request_scan: Callable[[str], None] | None = None,
        rating: RatingService | None = None,
        fresh_auth_s: int = 900,
        restricted_profile_exists: Callable[[], bool] = lambda: False,
        lock_wait: float = 5.0,
        store_dir: str | None = None,
        locate: Callable[[str], tuple[str, str]] | None = None,
    ) -> None:
        self._store = store_dir.rstrip("/") if store_dir else None
        self._locate = locate
        self._engine = engine
        self._fsops = fsops
        self._clock = clock
        self._scan = request_scan
        self._rating = rating or RatingService()
        self._fresh_auth_s = fresh_auth_s
        self._restricted = restricted_profile_exists
        self._locks = ResourceLocks(engine, clock=clock)
        self._lock_wait = lock_wait

    # -- the plan: reads only -----------------------------------------------------------------

    def plan(self, shelf_id: str | None = None) -> WithdrawalPlan:
        with self._engine.connect() as connection:
            return self._plan(connection, shelf_id)

    def adopted_unrated_plan(self, destination_id: str) -> WithdrawalPlan:
        """The plan of the unrated Works of an adopted child library; nothing moves. Refused on a
        read-only adoption root: Shelfhold never moves a file there, so the admin must do it."""
        with self._engine.connect() as connection:
            plan = adopted_unrated_plan(connection, destination_id)
            self._refuse_read_only(connection, plan)
            return plan

    def _refuse_read_only(self, connection: Connection, plan: WithdrawalPlan) -> None:
        for item in plan.items:
            for file in item.files:
                local = load_context(connection, file.destination_id).local(file.rel_path)
                if self._fsops.is_read_only(local):
                    raise WithdrawalRootReadOnlyError(
                        f"{local} is under a read-only adoption root: Shelfhold never moves it. "
                        "Mount the folder read-write (mode adopt, not adopt-ro) or take the files "
                        "out of the library yourself, then scan again."
                    )

    def _plan(self, connection: Connection, shelf_id: str | None) -> WithdrawalPlan:
        return build_plan(connection, self._rating, shelf_id)

    def _read(self, shelf_id: str | None, destination_id: str | None) -> WithdrawalPlan:
        if destination_id is not None:
            return self.adopted_unrated_plan(destination_id)
        return self.plan(shelf_id)

    # -- the application ----------------------------------------------------------------------

    def apply(
        self,
        *,
        if_match: str,
        principal: Principal,
        now: datetime,
        shelf_id: str | None = None,
        destination_id: str | None = None,
    ) -> WithdrawalReport:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["shelf.withdraw"], now=now,
            fresh_auth_s=self._fresh_auth_s,
            restricted_profile_exists=self._restricted(),
        )  # fmt: skip
        if not isinstance(verdict, Allowed):
            raise WithdrawalRefusedError(type(verdict).__name__)
        first = self._read(shelf_id, destination_id)
        if first.version != if_match:
            raise StalePlanError("the ratings moved since the plan was read: read it again")
        works = self._works_of(first)
        try:
            with (
                self._locks.hold_resources(
                    new_ulid(), [work_resource(w) for w in works], timeout=self._lock_wait
                )
                if works
                else nullcontext()
            ):
                # read again under the locks: an export that was writing has finished or waits
                current = self._read(shelf_id, destination_id)
                if current.version != if_match:
                    raise StalePlanError("a file arrived while the locks were taken: read again")
                if destination_id is not None:
                    return self._apply_adopted(current, principal, now)
                return self._apply_locked(current, principal, now)
        except LockBusyError as busy:
            raise WithdrawalBusyError(str(busy)) from None

    def _works_of(self, plan: WithdrawalPlan) -> list[str]:
        if not plan.items:
            return []
        with self._engine.connect() as connection:
            return sorted(
                set(
                    connection.execute(
                        sa.select(catalog.edition.c.work_id).where(
                            catalog.edition.c.id.in_([i.edition_id for i in plan.items])
                        )
                    ).scalars()
                )
            )

    def _apply_locked(
        self, plan: WithdrawalPlan, principal: Principal, now: datetime
    ) -> WithdrawalReport:
        journal = IntentJournal(self._engine, self._fsops, clock=self._clock)
        withdrawn: list[str] = []
        moved = 0
        pending: list[str] = []
        for item in plan.items:
            try:
                intents = self._commit(journal, item, principal, now)
            except FileChangedError:
                # a file keeps changing (a third party is writing it): nothing of this Edition
                # was committed, its files stay in place and wait, as when a move is refused; the
                # Editions before it are whole, and the report comes back
                with read_transaction(self._engine) as connection:
                    pending.extend(
                        load_context(connection, f.destination_id).local(f.rel_path)
                        for f in item.files
                    )
                continue
            withdrawn.append(item.edition_id)
            for local, intent_id in intents:
                if self._move(journal, local, intent_id):
                    moved += 1
                else:
                    pending.append(local)
            self._scan_folders(item)
        return WithdrawalReport(tuple(withdrawn), moved, tuple(pending))

    # -- adopted files: the only copy there is --------------------------------------------------

    def _apply_adopted(
        self, plan: WithdrawalPlan, principal: Principal, now: datetime
    ) -> WithdrawalReport:
        """An adopted file has no copy of Shelfhold's own: each gets a verified independent copy in
        /store before it is moved, and the database changes only after its move succeeded. A file
        that cannot be kept or moved stays where it is: record `adopted`, Edition shelved."""
        if self._store is None or self._locate is None:
            raise WithdrawalRootReadOnlyError("no /store: the originals could not be kept")
        journal = IntentJournal(self._engine, self._fsops, clock=self._clock)
        withdrawn: list[str] = []
        moved = 0
        pending: list[str] = []
        for item in plan.items:
            done_all = True
            for file in item.files:
                with self._engine.connect() as connection:
                    local = load_context(connection, file.destination_id).local(file.rel_path)
                if not self._keep_then_move(journal, file, local):
                    done_all = False
                    pending.append(local)
                    continue
                moved += 1
                with write_transaction(self._engine) as connection:
                    connection.execute(
                        sa.update(export.export_record)
                        .where(
                            export.export_record.c.shelf_destination_id == file.destination_id,
                            export.export_record.c.export_item_id == file.export_item_id,
                        )
                        .values(state="recycled")
                    )
            if done_all:
                self._unshelve(item, principal, now)
                withdrawn.append(item.edition_id)
            self._scan_folders(item)
        return WithdrawalReport(tuple(withdrawn), moved, tuple(pending))

    def _keep_then_move(self, journal: IntentJournal, file: WithdrawnFile, local: str) -> bool:
        observed = self._fsops.observe(local)
        if observed is None or observed[0] != "file":
            return True  # already gone from the library: nothing to move, the record may close
        try:
            if not self._keep_copy(file, local):
                return False
            # the whole file is read before the lock; under it, one `stat` says it is the same
            hashed = HashedFiles(self._fsops)
            if not file.sha256:
                hashed.hash(local)
            with write_transaction(self._engine) as connection:
                if file.sha256:
                    digest = file.sha256
                    now = self._fsops.observe(local)
                    if now is None or now[0] != "file" or now[1] != observed[1]:
                        raise FileChangedError(local)
                else:
                    checked = hashed.verified(local)
                    if checked is None or checked[0] != observed[1]:
                        raise FileChangedError(local)
                    digest = checked[1]
                intent = journal.record(
                    op="recycle", src=local, within=connection,
                    expected_sha256=digest, expected_file_id=observed[1],
                )  # fmt: skip
        except Exception:
            log.warning("withdrawal.keep_failed", export_item_id=file.export_item_id)
            return False
        return self._move(journal, local, intent)

    def _keep_copy(self, file: WithdrawnFile, local: str) -> bool:
        """A verified, independent copy in /store (a distinct inode, never a link), recorded."""
        if self._store is None or self._locate is None:
            return False
        digest = self._fsops.sha256_of(local)
        if file.sha256 and digest != file.sha256:
            return False  # changed since the plan was read: it is not the file that was shown
        source = self._fsops.link_stat(local)[:2]
        with self._engine.connect() as connection:
            if has_independent_store_copy(connection, self._fsops, digest, besides={source}):
                return True
            unit_id = connection.execute(
                sa.select(storage.content.c.unit_id)
                .select_from(
                    export.export_item.join(
                        storage.content, storage.content.c.id == export.export_item.c.content_id
                    )
                )
                .where(export.export_item.c.id == file.export_item_id)
            ).scalar_one_or_none()
        hexdigest = digest.removeprefix("sha256:")
        target = f"{self._store}/adopted/{hexdigest[:2]}/{file.export_item_id}.cbz"
        with self._fsops.open_read(local) as stream:
            self._fsops.write_atomic(
                target, stream, parents=True, replace=True,
                verify_digest=lambda: hexdigest,
            )  # fmt: skip
        if self._fsops.sha256_of(target) != digest:
            return False
        observed = self._fsops.observe(target)
        if observed is None:
            return False
        root_id, root_rel = self._locate(target)
        with write_transaction(self._engine) as connection:
            record_replica(
                connection, unit_id=unit_id, export_item_id=file.export_item_id, location="store",
                content_hash=digest, file_hash=digest, independent=True, physical_root_id=root_id,
                rel_path=root_rel, dev=self._fsops.device_of(target), ino=observed[1].ino,
                size=observed[1].size, mtime_ns=observed[1].mtime_ns,
                verified_at=self._clock.now(),
            )  # fmt: skip
        return True

    def _unshelve(self, item: WithdrawalItem, principal: Principal, now: datetime) -> None:
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(
                    catalog.edition.c.id == item.edition_id,
                    catalog.edition.c.shelf_id == item.shelf_id,
                )
                .values(shelf_id=None)
            )
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=principal.actor_id, action="shelf.withdraw", at=now,
                        target=f"edition:{item.edition_id};shelf:{item.shelf_id};{item.reason}",
                        session_id=principal.session_id, auth_time=principal.auth_time,
                    )
                )
            )  # fmt: skip

    def _move(self, journal: IntentJournal, local: str, intent_id: str) -> bool:
        """The file leaves; a failure is replayed at once through the intent (the same resolver as
        at start), and if it still fails the intent stays open for the next start."""
        try:
            journal.mark_applying(intent_id)
            self._fsops.recycle(local)
            journal.mark_done(intent_id)
            return True
        except Exception:
            log.warning("withdrawal.move_failed", intent_id=intent_id)
        try:
            report = journal.resolve_one(intent_id)
        except Exception:
            log.warning("withdrawal.move_pending", intent_id=intent_id)
            return False
        return report is None or intent_id in {*report.completed, *report.replayed}

    def _commit(
        self,
        journal: IntentJournal,
        item: WithdrawalItem,
        principal: Principal,
        now: datetime,
    ) -> list[tuple[str, str]]:
        """Intents, records, Edition and audit in one transaction."""

        def attempt() -> list[tuple[str, str]]:
            hashed = HashedFiles(self._fsops)
            with read_transaction(self._engine) as connection:
                paths = [
                    load_context(connection, f.destination_id).local(f.rel_path) for f in item.files
                ]
            for path in paths:  # the files are read before the lock, never under it
                hashed.hash(path)
            return self._commit_locked(journal, item, principal, now, paths, hashed)

        return retry_when_changed(attempt)

    def _commit_locked(
        self,
        journal: IntentJournal,
        item: WithdrawalItem,
        principal: Principal,
        now: datetime,
        paths: list[str],
        hashed: HashedFiles,
    ) -> list[tuple[str, str]]:
        record = export.export_record
        intents: list[tuple[str, str]] = []
        with write_transaction(self._engine) as connection:
            for file, expected in zip(item.files, paths, strict=True):
                local = load_context(connection, file.destination_id).local(file.rel_path)
                if local != expected:
                    raise FileChangedError(local)  # the rows moved since the look: start over
                checked = hashed.verified(local)  # one `stat` under the lock
                if checked is not None:
                    intents.append(
                        (
                            local,
                            journal.record(
                                op="recycle", src=local, within=connection,
                                expected_sha256=checked[1], expected_file_id=checked[0],
                            ),
                        )
                    )  # fmt: skip
                connection.execute(
                    sa.update(record)
                    .where(
                        record.c.shelf_destination_id == file.destination_id,
                        record.c.export_item_id == file.export_item_id,
                    )
                    .values(state="recycled")
                )
            connection.execute(
                sa.update(catalog.edition)
                .where(
                    catalog.edition.c.id == item.edition_id,
                    catalog.edition.c.shelf_id == item.shelf_id,
                )
                .values(shelf_id=None)
            )
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=principal.actor_id, action="shelf.withdraw", at=now,
                        target=f"edition:{item.edition_id};shelf:{item.shelf_id};{item.reason}",
                        session_id=principal.session_id, auth_time=principal.auth_time,
                    )
                )
            )  # fmt: skip
        return intents

    def _scan_folders(self, item: WithdrawalItem) -> None:
        """Kavita forgets a series by scanning the folder its files left (one scan per folder)."""
        if self._scan is None:
            return
        seen: set[str] = set()
        for file in item.files:
            folder = file.rel_path.split("/", 1)[0]
            with self._engine.connect() as connection:
                ctx = load_context(connection, file.destination_id)
            local = ctx.local(folder)
            if local not in seen:
                seen.add(local)
                self._scan(ctx.remote_folder(local))
