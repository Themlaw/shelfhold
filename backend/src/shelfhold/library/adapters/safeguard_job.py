"""\"Mettre à l'abri\" and the optional purge of the upstream copy (EF-BIB-038, EF-BIB-042 (4),
EF-SRC-009, EF-EXP-064).

Two halves, run in this order by the window job `library.safeguard`:

1. `SafeguardCycle`: the Units that are owed a copy in /store (their source is gone, or failed
   on content twice, EF-BIB-031) get an independent copy of the /library file, verified (distinct
   inode, `sha256`) before anything else is done. The work goes by capped batches (200 Units or
   5 GiB per cycle, and only what fits in /store above the disk guard's reserve); what is left
   is `unit.safeguard_backlog`, written for `status`. A Unit under a tombstone is never copied.
2. `UpstreamPurge`: for a shelf that asked for it (off by default, a reserved action to turn on),
   the download the service holds is let go of (`release_claim`) only when `upstream_verdict` says
   so: the copy Kavita reads exists, and a verified copy sits in /store, or the source is alive
   and nothing owes the Unit a copy. The deed is an intent (`purge_upstream`) that pins the
   `(dev, ino)`, the file hash and the `content_hash` of the /store copy the plan relied on, and
   looks at that copy again, at the moment of the deed, before the service is asked to erase
   anything (EF-EXP-064): a copy deleted, replaced or modified in between is a refusal.

A crash is harmless in both directions. After the copy and before the deed nothing upstream moved
and the next sweep plans again. After the deed and before the database knew, the next sweep asks
the service first: a chapter that is no longer there is only recorded, never erased twice; if the
/store copy was lost meanwhile there is nothing left to erase, and nothing is refused for it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.identity.public import AuditEntry, audit_insert
from shelfhold.infrastructure.archives import UnsafeArchiveError
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.content_trust import UntrustedEntryError
from shelfhold.infrastructure.fsops import FileIdentity, Fsops, FsopsError
from shelfhold.infrastructure.schema import acquisition, catalog, storage
from shelfhold.library.adapters.safeguard import payload_hash_at
from shelfhold.library.adapters.safeguard_inventory import (
    PurgeCandidate,
    SafeguardInventory,
    owed_unit_ids,
    record_backlog,
)
from shelfhold.library.adapters.storage import KeepCopier, verify_kept
from shelfhold.library.domain.content_hash import CONTENT_HASH_BY, FILE_HASH_BY
from shelfhold.library.domain.failures import SafeguardFailedError
from shelfhold.library.domain.safeguard_rules import (
    NOT_DOWNLOADED,
    REFUSALS,
    ROLES_UNVERIFIED,
    SERVICE_PAUSED,
    SOURCE_NOT_LOADED,
    STORE_COPY_CHANGED,
    STORE_COPY_MISSING,
    PurgeFacts,
    PurgeVerdict,
    upstream_verdict,
)
from shelfhold.library.domain.store_budget import (
    DEFAULT_BATCH_UNITS,
    Candidate,
    StoreBudget,
    plan_batch,
)
from shelfhold.library.replica import (
    StoreProof,
    library_copies,
    sibling_inodes,
    store_proofs,
    withdraw_upstream_claims,
)
from shelfhold.library.tombstones.values import ContentHash, SeriesBindingRef, VariantRef

__all__ = [
    "CycleReport",
    "IntentLog",
    "PinnedCopy",
    "PurgeDone",
    "PurgePlan",
    "PurgeRefused",
    "PurgeReport",
    "SafeguardCycle",
    "ServiceGate",
    "TombstoneScreen",
    "UpstreamPurge",
    "unit_tombstoned",
]

log = structlog.get_logger(__name__)

PURGE_OP = "purge_upstream"
Publish = Callable[[Connection, str, dict[str, object]], None]
# (connection, plugin id, upstream source key) -> why the service must not be asked to erase
# anything now (`service_paused`, `roles_unverified`, `source_not_loaded`), None when it may be
ServiceGate = Callable[[Connection, str, str], str | None]


class TombstoneScreen(Protocol):
    """The part of the tombstone gate the safeguard asks (`TombstoneGate` and `check_content`)."""

    def check(
        self,
        candidate: VariantRef | SeriesBindingRef,
        *,
        root_id: str | None,
        hash_with: Callable[[str], str | None] | None = None,
    ) -> Any: ...

    def check_content(
        self,
        hashes: Iterable[ContentHash] = (),
        *,
        hash_with: Callable[[str], str | None] | None = None,
    ) -> bool: ...


class IntentLog(Protocol):
    def record(
        self,
        *,
        op: str,
        src: str | None = None,
        dst: str | None = None,
        expected_sha256: str | None = None,
        expected_file_id: FileIdentity | None = None,
    ) -> str: ...

    def mark_applying(self, intent_id: str) -> None: ...

    def mark_done(self, intent_id: str, *, within: Connection | None = None) -> None: ...

    def cancel(self, intent_id: str) -> None: ...

    def supersede(self, op: str) -> int: ...


class UpstreamService(Protocol):
    async def poll(self, f: Any, t: Any) -> str: ...

    async def release(self, f: Any, t: Any) -> Any: ...


@dataclass(frozen=True)
class PinnedCopy(FileIdentity):
    """What the intent pins of the /store copy the deed relies on: the inode (`ino`, `dev`), the
    size and time it had, and the payload hash. The file hash is the intent's `expected_sha256`."""

    dev: int = 0
    content_hash: str = ""

    def to_json(self) -> dict[str, Any]:
        return {**super().to_json(), "dev": self.dev, "content_hash": self.content_hash}


# -- tombstones ------------------------------------------------------------------------------


def unit_tombstoned(connection: Connection, gate: TombstoneScreen, unit_id: str) -> bool:
    """A Unit whose reference, series or content a tombstone covers is neither kept nor erased
    upstream on any account (EF-BIB-038, EF-BIB-052)."""
    content, variant = storage.content, catalog.unit_variant
    binding, item = acquisition.source_binding, acquisition.source_item
    hashes: list[ContentHash] = []
    for row in connection.execute(
        sa.select(content.c.content_hash, content.c.original_sha256).where(
            content.c.unit_id == unit_id
        )
    ):
        if row.content_hash:
            hashes.append(ContentHash(CONTENT_HASH_BY, row.content_hash))
        hashes.append(ContentHash(FILE_HASH_BY, row.original_sha256))
    rows = connection.execute(
        sa.select(item.c.plugin_id, item.c.source_key, item.c.remote_id, variant.c.remote_unit_id)
        .select_from(
            variant.join(binding, binding.c.id == variant.c.source_binding_id).join(
                item, item.c.id == binding.c.source_item_id
            )
        )
        .where(variant.c.unit_id == unit_id)
    ).all()
    for r in rows:
        refs = (
            VariantRef(r.plugin_id, r.source_key, r.remote_unit_id, tuple(hashes)),
            SeriesBindingRef(r.plugin_id, r.source_key, r.remote_id),
        )
        if any(gate.check(ref, root_id=None).blocked for ref in refs):
            return True
    return bool(hashes) and gate.check_content(hashes)


# -- 1. the copy -----------------------------------------------------------------------------


@dataclass
class CycleReport:
    owed: int = 0
    tombstoned: int = 0
    selected: int = 0
    bytes: int = 0
    kept: int = 0
    failed: int = 0
    backlog: int = 0

    def counters(self) -> dict[str, int]:
        return {k: v for k, v in vars(self).items() if v}


class SafeguardCycle:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        keeper: KeepCopier,
        gate: TombstoneScreen,
        budget: Callable[[], StoreBudget],
        publish: Publish,
        clock: Clock,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._keeper = keeper
        self._gate = gate
        self._budget = budget
        self._publish = publish
        self._clock = clock
        self._inventory = SafeguardInventory(engine)
        self._failures: dict[str, int] = {}

    def plan(self, report: CycleReport) -> list[Candidate]:
        """The batch of this cycle. Says (and records) what stays behind; touches no file."""
        owed = self._inventory.owed_candidates()
        report.owed = len(owed)
        with self._engine.connect() as connection:
            kept = [c for c in owed if not unit_tombstoned(connection, self._gate, c.unit_id)]
        report.tombstoned = len(owed) - len(kept)
        batch = plan_batch(kept, self._budget()) if kept else plan_batch([], self._budget())
        report.selected, report.bytes, report.backlog = (
            len(batch.selected),
            batch.bytes,
            batch.backlog,
        )
        with self._engine.begin() as connection:
            record_backlog(
                connection, units=batch.backlog, size=sum(c.size for c in batch.deferred),
                now=self._clock.now(),
            )  # fmt: skip
            if batch.backlog:  # said once per run; the Units stay where they are, owed
                self._publish(connection, "unit.safeguard_backlog", {"count": batch.backlog})
        return list(batch.selected)

    def steps(self, batch: Sequence[Candidate], report: CycleReport) -> list[Callable[[], None]]:
        """One step per Unit, so a window that closes stops between two of them. A copy counts
        only once it was read back: distinct inode, the hash it was stored at (`verify_kept`)."""

        def step(unit_id: str) -> Callable[[], None]:
            def keep() -> None:
                try:
                    with self._engine.connect() as connection:
                        if unit_tombstoned(connection, self._gate, unit_id):
                            report.tombstoned += 1  # buried since the plan: left as it is
                            return
                    self._keeper.keep(unit_id)
                    if verify_kept(self._engine, self._fsops, unit_id) < 1:
                        raise SafeguardFailedError("not_independent", "the copy does not count")
                    report.kept += 1
                except SafeguardFailedError as error:
                    self._failures[error.reason] = self._failures.get(error.reason, 0) + 1
                    report.failed += 1
                    log.warning("safeguard.failed", unit=unit_id, reason=error.reason)

            return keep

        return [step(c.unit_id) for c in batch]

    def close(self, report: CycleReport) -> None:
        """One `unit.safeguard_failed` for the run, whatever the number of Units that failed."""
        if report.failed:
            with self._engine.begin() as connection:
                self._publish(
                    connection, "unit.safeguard_failed",
                    {"count": report.failed, "reasons": sorted(self._failures)},
                )  # fmt: skip
        self._failures = {}


# -- 2. the purge of the upstream copy ---------------------------------------------------------


class PurgeRefused(Exception):
    """The deed was refused at the moment of the doing; nothing upstream was touched."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"upstream purge refused: {reason}")
        if reason not in REFUSALS:
            raise ValueError(f"not a reason of the rule: {reason!r}")
        self.reason = reason


@dataclass(frozen=True)
class PurgePlan:
    candidate: PurgeCandidate
    pinned: StoreProof | None  # the /store copy the plan relied on; None: the live source


@dataclass(frozen=True)
class PurgeDone:
    unit_id: str
    already_gone: bool  # the service no longer held the chapter: only recorded
    freed_bytes: int | None  # measured by the service, never assumed (EF-SRC-031)
    still_present: bool | None


@dataclass
class PurgeReport:
    planned: int = 0
    purged: int = 0
    already_gone: int = 0
    refused: dict[str, int] = field(default_factory=dict)
    failed: int = 0
    freed_bytes: int = 0

    def refuse(self, reason: str) -> None:
        self.refused[reason] = self.refused.get(reason, 0) + 1

    def counters(self) -> dict[str, int]:
        found = {k: v for k, v in vars(self).items() if isinstance(v, int) and v}
        found.update({f"refused_{k}": v for k, v in self.refused.items()})
        return found


class UpstreamPurge:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        journal: IntentLog,
        gate: TombstoneScreen,
        service_gate: ServiceGate,
        service: UpstreamService,
        fetcher: Any,
        clock: Clock,
        *,
        ticket: Callable[[str], Any],
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._journal = journal
        self._gate = gate
        self._service_gate = service_gate
        self._service = service
        self._fetcher = fetcher
        self._clock = clock
        self._ticket = ticket  # token -> the SDK's `Ticket`
        self._inventory = SafeguardInventory(engine)

    # -- the facts, read from the database and the disks, never carried over -------------------

    def _assess(
        self, candidate: PurgeCandidate, *, store: bool | None, owed: set[str]
    ) -> tuple[PurgeVerdict, list[StoreProof]]:
        """The verdict on the facts as they are now. `store` None: look at /store and count the
        copies that verify; True: the caller pinned a copy and checked it itself."""
        with self._engine.connect() as connection:
            option_on, exports_verified = self._inventory.export_facts(
                connection, candidate.content_id
            )
            tombstoned = unit_tombstoned(connection, self._gate, candidate.unit_id)
            refusal = self._service_gate(connection, candidate.plugin_id, candidate.source_key)
            alive = self._inventory.source_alive(connection, candidate.unit_id)
            copies = list(library_copies(connection, candidate.unit_id))
            request_open = connection.execute(
                sa.select(acquisition.download_request.c.id).where(
                    acquisition.download_request.c.id == candidate.request_id,
                    acquisition.download_request.c.state == "fetched",
                    acquisition.download_request.c.ticket == candidate.ticket,
                )
            ).first()
        base = PurgeFacts(
            option_on=option_on and request_open is not None,
            tombstoned=tombstoned,
            service_paused=refusal == SERVICE_PAUSED,
            roles_unverified=refusal == ROLES_UNVERIFIED,
            source_loaded=refusal != SOURCE_NOT_LOADED,
            kavita_copy=True,
            store_copy=True,
            source_alive=alive,
            owed=candidate.unit_id in owed,
        )
        early = upstream_verdict(base)  # with both copies granted, only the database can refuse
        if not early.allowed:
            return early, []
        kavita = exports_verified and any(self._holds(c.local, c.file_hash) for c in copies)
        proofs: list[StoreProof] = []
        if store is None:
            verify_kept(self._engine, self._fsops, candidate.unit_id)  # a fresh look at the disk
            with self._engine.connect() as connection:
                proofs = store_proofs(connection, candidate.unit_id)
        held = bool(proofs) if store is None else store
        return upstream_verdict(replace(base, kavita_copy=kavita, store_copy=held)), proofs

    def _holds(self, local: str, file_hash: str | None) -> bool:
        if not file_hash:
            return False
        try:
            return self._fsops.sha256_of(local) == file_hash
        except (FileNotFoundError, FsopsError):
            return False

    def plan(self, candidate: PurgeCandidate, *, owed: set[str] | None = None) -> PurgePlan:
        """Decide from the facts now. Raises `PurgeRefused` with the reason; writes nothing."""
        if owed is None:
            with self._engine.connect() as connection:
                owed = owed_unit_ids(connection)
        verdict, proofs = self._assess(candidate, store=None, owed=owed)
        if not verdict.allowed:
            raise PurgeRefused(verdict.reason or STORE_COPY_MISSING)
        return PurgePlan(candidate, proofs[0] if proofs else None)

    # -- the deed ---------------------------------------------------------------------------

    async def execute(self, plan: PurgePlan) -> PurgeDone:
        """The intent first (it pins what the plan relied on), then the look at that very copy,
        then the service, then the database. The look is the point: the plan may be old."""
        candidate, pinned = plan.candidate, plan.pinned
        intent_id = self._journal.record(
            op="purge_upstream",
            src=pinned.local if pinned else None,
            expected_sha256=pinned.file_hash if pinned else None,
            expected_file_id=self._pin(pinned) if pinned else None,
        )  # fmt: skip
        ticket = self._ticket(candidate.ticket)
        try:
            if not await self._still_held(ticket):  # gone already: a crash after the deed
                done = PurgeDone(candidate.unit_id, True, None, None)
                self._settle(candidate, intent_id)
                return done
            self._reverify(plan)
        except BaseException:
            self._journal.cancel(intent_id)  # nothing was erased: nothing is left to settle
            raise
        self._journal.mark_applying(intent_id)
        result = await self._service.release(self._fetcher, ticket)
        # an unknown outcome (an exception above) leaves the intent `applying`: the next sweep
        # cancels it and asks the service before it does anything
        if not (getattr(result, "released", False) or getattr(result, "still_present", None)):
            self._journal.cancel(intent_id)
            raise PurgeRefused(NOT_DOWNLOADED)
        self._settle(candidate, intent_id)
        return PurgeDone(
            candidate.unit_id, False, getattr(result, "freed_bytes", None),
            getattr(result, "still_present", None),
        )  # fmt: skip

    def _pin(self, pinned: StoreProof) -> FileIdentity:
        found = self._fsops.observe(pinned.local)
        size, mtime = (found[1].size, found[1].mtime_ns) if found else (0, 0)
        return PinnedCopy(pinned.ino, size, mtime, dev=pinned.dev, content_hash=pinned.content_hash)

    async def _still_held(self, ticket: Any) -> bool:
        return str(await self._service.poll(self._fetcher, ticket)) == "ready"

    def _reverify(self, plan: PurgePlan) -> None:
        """The facts again, and the pinned copy as it is on the disk now (EF-EXP-064)."""
        candidate, pinned = plan.candidate, plan.pinned
        with self._engine.connect() as connection:
            owed = owed_unit_ids(connection)
        if pinned is not None:
            self._check_pinned(candidate.unit_id, pinned)
        verdict, _ = self._assess(candidate, store=pinned is not None, owed=owed)
        if not verdict.allowed:
            raise PurgeRefused(verdict.reason or STORE_COPY_MISSING)

    def _check_pinned(self, unit_id: str, pinned: StoreProof) -> None:
        found = self._fsops.observe(pinned.local)
        if found is None or found[0] != "file":
            raise PurgeRefused(STORE_COPY_MISSING)
        try:
            same_inode = (
                found[1].ino == pinned.ino and self._fsops.device_of(pinned.local) == pinned.dev
            )
            same_bytes = self._fsops.sha256_of(pinned.local) == pinned.file_hash
            same_payload = payload_hash_at(self._fsops, pinned.local) == pinned.content_hash
        except (FileNotFoundError, FsopsError, UnsafeArchiveError, UntrustedEntryError):
            raise PurgeRefused(STORE_COPY_CHANGED) from None
        with self._engine.connect() as connection:
            linked = (pinned.dev, pinned.ino) in sibling_inodes(connection, unit_id)
        if not (same_inode and same_bytes and same_payload) or linked:
            raise PurgeRefused(STORE_COPY_CHANGED)

    def _settle(self, candidate: PurgeCandidate, intent_id: str) -> None:
        """The database learns it, in one transaction with the end of the intent: the ticket is
        let go of (the next sweep never asks again), Shelfhold no longer counts the origin as a
        copy of its own, the audit says which Unit (an opaque identifier, never a title)."""
        now = self._clock.now()
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(acquisition.download_request)
                .where(
                    acquisition.download_request.c.id == candidate.request_id,
                    acquisition.download_request.c.ticket == candidate.ticket,
                )
                .values(ticket=None, updated_at=now)
            )
            withdraw_upstream_claims(connection, [candidate.unit_id])
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=None, action="unit.upstream_purged", at=now,
                        target=f"unit:{candidate.unit_id}",
                    )
                )
            )  # fmt: skip
            self._journal.mark_done(intent_id, within=connection)

    async def sweep(
        self,
        *,
        awake: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        limit: int = DEFAULT_BATCH_UNITS,
    ) -> PurgeReport:
        report = PurgeReport()
        self._journal.supersede(PURGE_OP)  # an intent left `applying`: this sweep asks again
        with self._engine.connect() as connection:
            owed = owed_unit_ids(connection)
        plans: list[PurgePlan] = []
        for candidate in self._inventory.upstream_candidates():
            if len(plans) >= limit:
                break
            try:
                plans.append(self.plan(candidate, owed=owed))
            except PurgeRefused as refused:
                report.refuse(refused.reason)
        report.planned = len(plans)
        if not plans:
            return report
        async with awake() if awake is not None else nullcontext():
            for plan in plans:
                try:
                    done = await self.execute(plan)
                except PurgeRefused as refused:
                    report.refuse(refused.reason)
                    continue
                except Exception as error:  # the service did not answer: asked again next time
                    log.warning("safeguard.purge_failed", error_type=type(error).__name__)
                    report.failed += 1
                    continue
                if done.already_gone:
                    report.already_gone += 1
                else:
                    report.purged += 1
                    report.freed_bytes += done.freed_bytes or 0
        return report
