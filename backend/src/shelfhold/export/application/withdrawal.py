"""The removal plan of a family shelf (EF-BIB-037, EF-BIB-055): what leaves, shown before it goes.

An Edition filed on a shelf below 18+ that the admission now refuses (a Work rated higher, an
uncensored Edition rated 18+, a suggested age above its rating, a shelf lowered) must leave the
library the children can read. The plan lists those Editions and the files they hold; nothing moves
until an admin applies it, by a fresh session or the host CLI (`shelf.withdraw`, never a key), with
the version of the plan they read (`plan.stale`, 412, when anything moved since).

Applying is, per Edition and in this order, the safe one:

1. an intent (`recycle`) per file, committed with the hash of the file it is for;
2. one transaction: the records become `recycled`, the Edition is unshelved ("À classer"), the audit
   entry is written;
3. the file moves into the bin of its root (`.shelfhold/recycle`, never deleted, restorable) and
   Kavita is asked to scan the folder the file left, which is how it forgets the series.

A crash after 2 leaves the intent: the next start finishes the move of that file and no other.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import RatingService
from shelfhold.export.application.intents import IntentJournal
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
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import catalog, export

__all__ = [
    "WithdrawalItem",
    "WithdrawalPlan",
    "WithdrawalRefusedError",
    "WithdrawalReport",
    "WithdrawalService",
    "WithdrawnFile",
    "build_plan",
]

log = structlog.get_logger(__name__)

LIVE_STATES = ("written", "scanned", "resolved", "adopted")  # a file may be in the shelf


class WithdrawalRefusedError(Exception):
    """The caller may not apply a removal plan (the verdict of `authorize_reserved`)."""


@dataclass(frozen=True)
class WithdrawnFile:
    destination_id: str
    export_item_id: str
    rel_path: str


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
    """Every Edition filed on a shelf below 18+ that the admission refuses now, with its files."""
    ceilings = dict(
        connection.execute(sa.select(export.shelf.c.id, export.shelf.c.max_age_rating)).all()
    )
    items = [
        WithdrawalItem(
            w.edition_id, w.shelf_id, w.reason.value, _files(connection, w.edition_id, w.shelf_id)
        )
        for w in rating.withdrawals(connection, shelf_id=shelf_id)
        if is_family(ceilings[w.shelf_id])  # an adult shelf keeps what it holds
    ]
    material = json.dumps([asdict(i) for i in items], sort_keys=True, separators=(",", ":"))
    return WithdrawalPlan(tuple(items), hashlib.sha256(material.encode()).digest()[:16].hex())


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
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._clock = clock
        self._scan = request_scan
        self._rating = rating or RatingService()
        self._fresh_auth_s = fresh_auth_s
        self._restricted = restricted_profile_exists

    # -- the plan: reads only -----------------------------------------------------------------

    def plan(self, shelf_id: str | None = None) -> WithdrawalPlan:
        with self._engine.connect() as connection:
            return self._plan(connection, shelf_id)

    def _plan(self, connection: Connection, shelf_id: str | None) -> WithdrawalPlan:
        return build_plan(connection, self._rating, shelf_id)

    # -- the application ----------------------------------------------------------------------

    def apply(
        self,
        *,
        if_match: str,
        principal: Principal,
        now: datetime,
        shelf_id: str | None = None,
    ) -> WithdrawalReport:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["shelf.withdraw"], now=now,
            fresh_auth_s=self._fresh_auth_s,
            restricted_profile_exists=self._restricted(),
        )  # fmt: skip
        if not isinstance(verdict, Allowed):
            raise WithdrawalRefusedError(type(verdict).__name__)
        current = self.plan(shelf_id)
        if current.version != if_match:
            raise StalePlanError("the ratings moved since the plan was read: read it again")
        journal = IntentJournal(self._engine, self._fsops, clock=self._clock)
        withdrawn: list[str] = []
        moved = 0
        pending: list[str] = []
        for item in current.items:
            intents = self._intents(journal, item)
            self._commit(item, intents, principal, now)
            withdrawn.append(item.edition_id)
            for local, intent_id in intents:
                try:
                    journal.mark_applying(intent_id)
                    self._fsops.recycle(local)
                    journal.mark_done(intent_id)
                    moved += 1
                except Exception:  # the intent stays open: the next start finishes this file
                    log.warning("withdrawal.move_pending", intent_id=intent_id)
                    pending.append(local)
            self._scan_folders(item)
        return WithdrawalReport(tuple(withdrawn), moved, tuple(pending))

    def _intents(self, journal: IntentJournal, item: WithdrawalItem) -> list[tuple[str, str]]:
        found = []
        for file in item.files:
            with self._engine.connect() as connection:
                local = load_context(connection, file.destination_id).local(file.rel_path)
            observed = self._fsops.observe(local)
            if observed is None or observed[0] != "file":
                continue  # not there any more: only the rows are left to settle
            intent = journal.record(
                op="recycle", src=local, expected_sha256=self._fsops.sha256_of(local),
                expected_file_id=observed[1],
            )  # fmt: skip
            found.append((local, intent))
        return found

    def _commit(
        self,
        item: WithdrawalItem,
        intents: list[tuple[str, str]],
        principal: Principal,
        now: datetime,
    ) -> None:
        record = export.export_record
        with self._engine.begin() as connection:
            for file in item.files:
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
                .where(catalog.edition.c.id == item.edition_id)
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
