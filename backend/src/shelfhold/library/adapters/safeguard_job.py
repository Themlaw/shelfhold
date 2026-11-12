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
from datetime import datetime
from typing import Any, Protocol, cast

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.identity.public import AuditEntry, audit_insert
from shelfhold.infrastructure.archives import UnsafeArchiveError
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.content_trust import UntrustedEntryError
from shelfhold.infrastructure.fsops import FileIdentity, Fsops, FsopsError
from shelfhold.infrastructure.schema import acquisition, catalog, coherence, platform, storage
from shelfhold.library.adapters.safeguard import payload_hash_at
from shelfhold.library.adapters.safeguard_inventory import (
    PurgeCandidate,
    SafeguardInventory,
    content_hash_of,
    owed_unit_ids,
    record_backlog,
)
from shelfhold.library.adapters.storage import KeepCopier, verify_kept
from shelfhold.library.domain.content_hash import CONTENT_HASH_BY, FILE_HASH_BY
from shelfhold.library.domain.download_ticket import DownloadTicket
from shelfhold.library.domain.failures import SafeguardFailedError
from shelfhold.library.domain.safeguard_rules import (
    DEED_OPEN,
    NOT_DOWNLOADED,
    REFUSALS,
    ROLES_UNVERIFIED,
    SERVICE_PAUSED,
    SOURCE_NOT_LOADED,
    STORE_COPY_CHANGED,
    STORE_COPY_MISSING,
    PurgeFacts,
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
    fresh_library_inodes,
    is_own_inode,
    library_copies,
    sibling_inodes,
    store_proofs,
    withdraw_upstream_claims,
)
from shelfhold.library.tombstones.values import ContentHash, SeriesBindingRef, VariantRef

__all__ = [
    "CycleReport",
    "DownloadTicket",
    "IntentLog",
    "PinnedCopy",
    "PurgeDone",
    "PurgePlan",
    "PurgeRefused",
    "PurgeReport",
    "PurgeUnresolvedError",
    "ReleaseOutcome",
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

    def mark_applying(self, intent_id: str, *, within: Connection | None = None) -> None: ...

    def open_payloads(self, op: str) -> list[tuple[str, str, dict[str, Any]]]: ...

    def mark_done(self, intent_id: str, *, within: Connection | None = None) -> None: ...

    def cancel(self, intent_id: str) -> None: ...


class UpstreamService(Protocol):
    async def poll(self, f: Any, t: Any) -> str: ...

    async def release(self, f: Any, t: Any) -> Any: ...


class QueueService(UpstreamService, Protocol):
    """What `purge --now` asks of the service on top of the above: take a queued chapter out of
    its queue (`cancel`, EF-SRC-033)."""

    async def cancel(self, f: Any, t: Any) -> None: ...


@dataclass(frozen=True)
class ReleaseOutcome:
    kind: str  # released | gone | cancelled | kept_by_other_account
    freed_bytes: int | None = None


KEPT_PREFIX = "purge.kept_in_service:"  # `app_setting`: one key per request (an opaque ULID)


def mark_kept_in_service(connection: Connection, request_id: str, at: datetime) -> None:
    """A download the service still holds after a purge let go of Shelfhold's claim: the admin
    deletes it there, then clears the mark. Idempotent."""
    connection.execute(
        sqlite_insert(platform.app_setting)
        .values(key=KEPT_PREFIX + request_id, value=at.isoformat())
        .on_conflict_do_nothing(index_elements=["key"])
    )


class PurgeUnresolvedError(Exception):
    """A deed on this download was begun and nobody knows whether it landed (or another deed is
    about to): it is never sent again, the admin decides (`intents list`, `intents resolve`)."""


@dataclass(frozen=True)
class PinnedCopy(FileIdentity):
    """What the intent writes of the deed: the request it is for (so that a deed that was begun
    is found again, whatever happens next), and, when a /store copy was what the plan relied on,
    its inode (`ino`, `dev`), size and time, and payload hash. The file hash is the intent's
    `expected_sha256`. `dev = 0` and no `content_hash`: the plan relied on a live source."""

    dev: int = 0
    content_hash: str = ""
    request_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            **super().to_json(),
            "dev": self.dev,
            "content_hash": self.content_hash,
            "request_id": self.request_id,
        }


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
        self._inventory = SafeguardInventory(engine, fsops)
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
    seen: tuple[int, int] | None = None  # its (mtime_ns, ctime_ns) when the plan looked


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
    handed_to_admin: int = 0  # begun deeds whose outcome is ambiguous (`intents list`)
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
        self._inventory = SafeguardInventory(engine, fsops)

    # -- the facts, read from the database and the disks, never carried over -------------------

    def _facts(
        self, connection: Connection, candidate: PurgeCandidate, owed: set[str]
    ) -> tuple[PurgeFacts, bool, str | None]:
        """What the database says now: the rule's facts (copies granted), whether every export of
        this content was seen in Kavita, and the payload hash of this very content."""
        option_on, exports_verified = self._inventory.export_facts(connection, candidate.content_id)
        refusal = self._service_gate(connection, candidate.plugin_id, candidate.source_key)
        request_open = connection.execute(
            sa.select(acquisition.download_request.c.id).where(
                acquisition.download_request.c.id == candidate.request_id,
                acquisition.download_request.c.state == "fetched",
                acquisition.download_request.c.ticket == candidate.ticket,
            )
        ).first()
        facts = PurgeFacts(
            option_on=option_on and request_open is not None,
            tombstoned=unit_tombstoned(connection, self._gate, candidate.unit_id),
            service_paused=refusal == SERVICE_PAUSED,
            roles_unverified=refusal == ROLES_UNVERIFIED,
            source_loaded=refusal != SOURCE_NOT_LOADED,
            kavita_copy=True,
            store_copy=True,
            source_alive=self._inventory.source_alive(connection, candidate.unit_id),
            owed=candidate.unit_id in owed,
        )
        return facts, exports_verified, content_hash_of(connection, candidate.content_id)

    def _kavita_on_disk(self, candidate: PurgeCandidate, content_hash: str | None) -> bool:
        """The file Kavita reads, **of this content**, is there and is what was exported."""
        if not content_hash:
            return False
        with self._engine.connect() as connection:
            copies = [
                c
                for c in library_copies(connection, candidate.unit_id)
                if c.content_hash == content_hash
            ]
        return any(self._holds(c.local, c.file_hash) for c in copies)

    def _holds(self, local: str, file_hash: str | None) -> bool:
        if not file_hash:
            return False
        try:
            return self._fsops.sha256_of(local) == file_hash
        except (FileNotFoundError, FsopsError):
            return False

    def _stamps(self, proof: StoreProof) -> tuple[int, int] | None:
        """When the file was last written and last changed, as the disk says now. A replacement
        gets new ones even if the system hands the freed inode number out again."""
        try:
            return self._fsops.link_stat(proof.local)[3:5]
        except (FileNotFoundError, FsopsError):
            return None

    def _usable_proof(
        self, candidate: PurgeCandidate, content_hash: str | None
    ) -> StoreProof | None:
        """The first /store copy **of this content** that is one of its own on the disk now."""
        if not content_hash:
            return None
        verify_kept(self._engine, self._fsops, candidate.unit_id)  # a fresh look at the disk
        with self._engine.connect() as connection:
            proofs = [
                p
                for p in store_proofs(connection, candidate.unit_id)
                if p.content_hash == content_hash
            ]
            siblings = fresh_library_inodes(connection, self._fsops, candidate.unit_id)
        for proof in proofs:
            if not is_own_inode(self._fsops, proof.local, siblings)[0]:
                continue  # one inode with a library name, or two names: no copy of its own
            try:
                self._check_pinned(candidate.unit_id, proof)
            except PurgeRefused:
                continue
            return proof
        return None

    def plan(self, candidate: PurgeCandidate, *, owed: set[str] | None = None) -> PurgePlan:
        """Decide from the facts now. Raises `PurgeRefused` with the reason; writes nothing."""
        with self._engine.connect() as connection:
            if owed is None:
                owed = owed_unit_ids(connection)
            facts, exports_verified, content_hash = self._facts(connection, candidate, owed)
        early = upstream_verdict(facts)  # with both copies granted, only the database can refuse
        if not early.allowed:
            raise PurgeRefused(early.reason or STORE_COPY_MISSING)
        kavita = exports_verified and self._kavita_on_disk(candidate, content_hash)
        proof = self._usable_proof(candidate, content_hash)
        verdict = upstream_verdict(replace(facts, kavita_copy=kavita, store_copy=proof is not None))
        if not verdict.allowed:
            raise PurgeRefused(verdict.reason or STORE_COPY_MISSING)
        return PurgePlan(candidate, proof, self._stamps(proof) if proof else None)

    # -- the deed ---------------------------------------------------------------------------

    async def execute(self, plan: PurgePlan) -> PurgeDone:
        """The intent first (it names the request and pins what the plan relied on), then the
        look at that very copy and at the facts, then the mark `applying` in the transaction that
        looked, then the service, then the database. The look is the point: the plan may be old,
        and a consent withdrawn a moment ago must stop the deed (the transaction holds the write
        lock, so nothing changes between the look and the mark)."""
        candidate, pinned = plan.candidate, plan.pinned
        intent_id = self._journal.record(
            op="purge_upstream",
            src=pinned.local if pinned else None,
            expected_sha256=pinned.file_hash if pinned else None,
            expected_file_id=self._pin(plan),
        )  # fmt: skip
        ticket = self._ticket(candidate.ticket)
        try:
            if not await self._still_held(ticket):  # gone already: a deed that did land
                self._settle(candidate, intent_id)
                return PurgeDone(candidate.unit_id, True, None, None)
            kavita_disk = self._look_at_disks(plan)
            self._decide_and_mark(plan, intent_id, kavita_disk)
        except BaseException:
            self._journal.cancel(intent_id)  # nothing was erased: nothing is left to settle
            raise
        # From here an unknown outcome (a kill, an exception) leaves the intent `applying`, and
        # the next sweep never sends the erasure again: it asks the service, then settles or
        # hands the Unit to the admin.
        result = await self._service.release(self._fetcher, ticket)
        if not (getattr(result, "released", False) or getattr(result, "still_present", None)):
            self._journal.cancel(intent_id)
            raise PurgeRefused(NOT_DOWNLOADED)
        self._settle(candidate, intent_id)
        return PurgeDone(
            candidate.unit_id, False, getattr(result, "freed_bytes", None),
            getattr(result, "still_present", None),
        )  # fmt: skip

    def _pin(self, plan: PurgePlan) -> FileIdentity:
        pinned = plan.pinned
        request = plan.candidate.request_id
        if pinned is None:
            return PinnedCopy(0, 0, 0, request_id=request)
        found = self._fsops.observe(pinned.local)
        size, mtime = (found[1].size, found[1].mtime_ns) if found else (0, 0)
        return PinnedCopy(
            pinned.ino, size, mtime, dev=pinned.dev, content_hash=pinned.content_hash,
            request_id=request,
        )  # fmt: skip

    async def _still_held(self, ticket: Any) -> bool:
        return str(await self._service.poll(self._fetcher, ticket)) == "ready"

    def _look_at_disks(self, plan: PurgePlan) -> bool:
        """The disks as they are now: the pinned /store copy, and the file Kavita reads."""
        candidate, pinned = plan.candidate, plan.pinned
        if pinned is not None:
            self._check_pinned(candidate.unit_id, pinned, plan.seen)
        with self._engine.connect() as connection:
            content_hash = content_hash_of(connection, candidate.content_id)
        return self._kavita_on_disk(candidate, content_hash)

    def _decide_and_mark(self, plan: PurgePlan, intent_id: str, kavita_disk: bool) -> None:
        """The facts of the database once more and `mark_applying`, in one `BEGIN IMMEDIATE`."""
        candidate, pinned = plan.candidate, plan.pinned
        immediate = self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
        with immediate.begin() as connection:
            owed = owed_unit_ids(connection)
            facts, exports_verified, content_hash = self._facts(connection, candidate, owed)
            if pinned is not None and pinned.content_hash != content_hash:
                raise PurgeRefused(STORE_COPY_CHANGED)  # not a copy of this content
            verdict = upstream_verdict(
                replace(
                    facts,
                    kavita_copy=exports_verified and kavita_disk,
                    store_copy=pinned is not None,
                )
            )
            if not verdict.allowed:
                raise PurgeRefused(verdict.reason or STORE_COPY_MISSING)
            if self._other_deed_open(connection, candidate.request_id, intent_id):
                raise PurgeRefused(DEED_OPEN)  # `purge --now` is on this very download
            self._journal.mark_applying(intent_id, within=connection)

    @staticmethod
    def _other_deed_open(connection: Connection, request_id: str, intent_id: str) -> bool:
        """Another erasure of this download is begun: the service takes a second one for the
        withdrawal of another account's claim, so a download gets one deed at a time."""
        table = coherence.fs_intent
        for _other, payload in connection.execute(
            sa.select(table.c.id, table.c.expected_file_id).where(
                table.c.op == PURGE_OP, table.c.state == "applying", table.c.id != intent_id
            )
        ):
            if str((payload or {}).get("request_id") or "") == request_id:
                return True
        return False

    # -- `purge --now`: the download of a Unit being erased (EF-BIB-052, EF-SRC-033) -----------

    async def release_for_purge(self, target: DownloadTicket) -> ReleaseOutcome:
        """Let the service go of the download of a Unit the admin is purging, by the road of the
        deed above: an intent that names the request, the service asked first, `release` sent once
        and never again. No shelf option and no /store copy is asked for (the admin typed the
        identifier and the tombstone is down), but the service gate is: a paused, unverified or
        unloaded service is not given a delete. A queued chapter is taken out of its queue."""
        with self._engine.connect() as connection:
            refusal = self._service_gate(connection, target.plugin_id, target.source_key)
        if refusal:
            raise PurgeRefused(refusal)
        ticket = self._ticket(target.ticket)
        for intent_id, state, payload in self._journal.open_payloads(PURGE_OP):
            if str(payload.get("request_id") or "") != target.request_id:
                continue
            if state == "pending":
                self._journal.cancel(intent_id)  # recorded, never begun
                continue
            held = await self._still_held(ticket)
            # begun: never sent again, whoever began it. Gone: it landed. Still there: not sent,
            # or (`ui_login`) another account keeps the file; nothing tells which, so the claim
            # is let go of and the admin is told to delete in the service. The ticket goes with
            # the intent: no door (`intents resolve`, `--abandon`) leads to a second erasure.
            self._settle(target, intent_id, kept=held)
            return ReleaseOutcome("kept_by_other_account" if held else "gone")
        if target.state == "requested":
            await cast("QueueService", self._service).cancel(self._fetcher, ticket)
        if not await self._still_held(ticket):
            self._settle(target, None)  # nothing downloaded: the queue entry (if any) is gone
            return ReleaseOutcome("cancelled" if target.state == "requested" else "gone")
        intent_id = self._journal.record(
            op="purge_upstream",
            expected_file_id=PinnedCopy(0, 0, 0, request_id=target.request_id),
        )
        try:
            self._mark_alone(target, intent_id)
        except BaseException:
            self._journal.cancel(intent_id)  # nothing was erased
            raise
        # From here an unknown outcome leaves the intent `applying`: see `release_for_purge`.
        result = await self._service.release(self._fetcher, ticket)
        present = getattr(result, "still_present", None)
        if not (getattr(result, "released", False) or present):
            self._journal.cancel(intent_id)  # it was not downloaded after all
            self._settle(target, None)
            return ReleaseOutcome("gone")
        self._settle(target, intent_id, kept=bool(present))
        return ReleaseOutcome(
            "kept_by_other_account" if present else "released", getattr(result, "freed_bytes", None)
        )

    def _mark_alone(self, target: DownloadTicket, intent_id: str) -> None:
        """`applying`, in a locked transaction that looks once more: the ticket is still the
        request's and no other deed holds it."""
        immediate = self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
        table = acquisition.download_request
        with immediate.begin() as connection:
            held = connection.execute(
                sa.select(table.c.id).where(
                    table.c.id == target.request_id, table.c.ticket == target.ticket
                )
            ).first()
            if held is None:
                raise PurgeUnresolvedError(target.request_id)  # settled meanwhile: not ours to send
            if self._other_deed_open(connection, target.request_id, intent_id):
                raise PurgeUnresolvedError(target.request_id)
            self._journal.mark_applying(intent_id, within=connection)

    def _check_pinned(
        self, unit_id: str, pinned: StoreProof, seen: tuple[int, int] | None = None
    ) -> None:
        """The copy in /store is the one that was proved, and no other name of the Unit is the
        same file: read from the disk (`lstat`), never from the registry."""
        found = self._fsops.observe(pinned.local)
        if found is None or found[0] != "file":
            raise PurgeRefused(STORE_COPY_MISSING)
        try:
            dev, ino, links, mtime, ctime = self._fsops.link_stat(pinned.local)
            same_bytes = self._fsops.sha256_of(pinned.local) == pinned.file_hash
            same_payload = payload_hash_at(self._fsops, pinned.local) == pinned.content_hash
        except (FileNotFoundError, FsopsError, UnsafeArchiveError, UntrustedEntryError):
            raise PurgeRefused(STORE_COPY_CHANGED) from None
        if (
            (dev, ino) != (pinned.dev, pinned.ino)
            or links != 1
            or (seen is not None and (mtime, ctime) != seen)  # same number, another file
            or not (same_bytes and same_payload)
        ):
            raise PurgeRefused(STORE_COPY_CHANGED)
        with self._engine.connect() as connection:
            names = [c.local for c in library_copies(connection, unit_id)]
            recorded = sibling_inodes(connection, unit_id)
        if (dev, ino) in recorded:
            raise PurgeRefused(STORE_COPY_CHANGED)
        for name in names:
            try:
                other = self._fsops.link_stat(name)[:2]
            except (FileNotFoundError, FsopsError):
                continue  # a name that is not there is not the same file
            if other == (dev, ino):
                raise PurgeRefused(STORE_COPY_CHANGED)

    def _settle(
        self,
        candidate: PurgeCandidate | DownloadTicket,
        intent_id: str | None,
        *,
        kept: bool = False,
    ) -> None:
        """The database learns it, in one transaction with the end of the intent: the ticket is
        let go of (the next sweep never asks again), Shelfhold no longer counts the origin as a
        copy of its own, the audit says which Unit (an opaque identifier, never a title). `kept`:
        the file stays in the service (another account may hold it, or the answer was ambiguous):
        a durable mark, which `status` shows until the admin clears it (`purge --kept-ack`)."""
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
            # a Unit that is gone (the leftovers of an M1a purge) is named by its request
            target = (
                f"unit:{candidate.unit_id}"
                if candidate.unit_id
                else f"request:{candidate.request_id}"
            )
            connection.execute(
                audit_insert(
                    AuditEntry(actor_id=None, action="unit.upstream_purged", at=now, target=target)
                )
            )  # fmt: skip
            if kept:
                mark_kept_in_service(connection, candidate.request_id, now)
            if intent_id is not None:
                self._journal.mark_done(intent_id, within=connection)

    # -- a deed that was begun: asked of the service, never sent again --------------------------

    async def _reconcile(self, intent_id: str, request_id: str, report: PurgeReport) -> None:
        """An intent left `applying` (a kill, an unanswered call). The erasure is **not** sent
        again: in `ui_login` the service would take it for the download another account keeps.
        The service is asked what is there. Gone: the deed landed, the database is told (this
        needs no /store copy, which may be lost since). Still there: the outcome is ambiguous
        (not sent, or another account holds it), so the intent stays open for the admin
        (`intents list`, `intents resolve`) and the Unit is left alone."""
        found = self._inventory.upstream_candidates(request_id=request_id) if request_id else []
        if not found:  # the ticket is already let go of, or no request is named: nothing to ask
            report.handed_to_admin += 1
            return
        if await self._still_held(self._ticket(found[0].ticket)):
            # not sent, or another account keeps the file: nothing tells which. The claim is let
            # go of with the intent (a second DELETE would take it for the other account's), and
            # the download is left in the service for the admin to delete there.
            log.warning("safeguard.purge_ambiguous", intent=intent_id)
            self._settle(found[0], intent_id, kept=True)
            report.handed_to_admin += 1
            return
        self._settle(found[0], intent_id)
        report.already_gone += 1

    async def sweep(
        self,
        *,
        awake: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        limit: int = DEFAULT_BATCH_UNITS,
    ) -> PurgeReport:
        report = PurgeReport()
        begun: list[tuple[str, str]] = []  # (intent, request) of the deeds left `applying`
        for intent_id, state, payload in self._journal.open_payloads(PURGE_OP):
            if state == "pending":
                self._journal.cancel(intent_id)  # recorded, never begun: nothing was touched
            else:
                begun.append((intent_id, str(payload.get("request_id") or "")))
        with self._engine.connect() as connection:
            owed = owed_unit_ids(connection)
        skip = {request for _, request in begun}
        plans: list[PurgePlan] = []
        for candidate in self._inventory.upstream_candidates():
            if len(plans) >= limit:
                break
            if candidate.request_id in skip:
                continue  # a deed on it is open: settled or handed over, never decided anew
            try:
                plans.append(self.plan(candidate, owed=owed))
            except PurgeRefused as refused:
                report.refuse(refused.reason)
        report.planned = len(plans)
        if not plans and not begun:
            return report
        async with awake() if awake is not None else nullcontext():
            for intent_id, request in begun:
                try:
                    await self._reconcile(intent_id, request, report)
                except Exception as error:  # the service did not answer: asked again next time
                    log.warning("safeguard.purge_failed", error_type=type(error).__name__)
                    report.failed += 1
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
