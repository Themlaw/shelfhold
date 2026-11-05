"""`shelfhold purge --now` (EF-BIB-052, EF-BIB-053): erase a Unit, or a whole Work, for good.

The order is the safety. Each step is durable before the next one starts, and a crash anywhere
leaves a state the same command finishes:

1. **the tombstone**, in the database and in the journal of every mounted read-write root
   (`TombstoneService.record`: the row, the pending line per root, then the `fsync`ed line). A root
   that is away keeps its line as a pending row, written at its return before anything else. From
   here on the content cannot be acquired again, by any road, whatever happens next;
2. **the fsync**: `record` returns once the lines are on disk (the journal appends and syncs);
3. **the intent** of each deletion (`fs_intent`, `unlink` or `delete`), committed before the disk
   is touched and marked `applying`;
4. **the deletion**, one file at a time through `Fsops.purge_unlink` (never a folder, never a link,
   never a host file, never a read-only root), the Unit's bin entries, then `/data` (its
   `unit.json`, thumbnails and content cache) and its lines in the provenance mirror;
5. **the closing**, in one transaction: the intents `done`, the open download tickets
   `closed_by_purge` (Shelfhold side only), the upstream claim withdrawn, the rows of the Unit
   erased (Variants, contents, copies, export items, reading positions), the audit entry and the
   event. The rows go only if every copy is gone: otherwise the report says what remains, the rows
   that name it are kept, and the same command is run again once the root is back.

The purge is a reserved action (`purge.now`): a fresh admin session or the host CLI, never a key,
and it re-checks the rule here, not only at the route. The admin types the identifier. It passes
over the bin, "Mettre à l'abri" and the retained-backup rule for the managed copies. Nothing is
ever written to the upstream service: it withdraws Shelfhold's claim and says "delete in
Suwayomi" (M1a; `release_claim` arrives in M1b). Nothing here carries a title: the audit target and
the event hold ULIDs, the tombstone reason is an enumeration.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks, path_resource, work_resource
from shelfhold.export.domain.physical_roots import RECYCLE_DIR, RootState
from shelfhold.export.domain.provenance_mirror import (
    MIRROR_DIR,
    MirrorDocument,
    MirrorFormatError,
    drop_unit_lines,
)
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    Principal,
    Verdict,
    authorize_reserved,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import (
    FileIdentity,
    Fsops,
    FsopsError,
    NotRegularFileError,
    OutsidePathMapError,
    PathEscapeError,
    ReadOnlyRootError,
    ReservedPathError,
    RootBlockedError,
    TombstonePendingError,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import (
    acquisition,
    catalog,
    coherence,
    export,
    identity,
    platform,
    reading,
)
from shelfhold.infrastructure.volumes import erase_data_path
from shelfhold.library.public import (
    EDITION_KEY_COLUMNS,
    ConfirmationRequiredError,
    ContentHash,
    Copy,
    PlaceRef,
    SeriesBindingRef,
    TombstoneRoot,
    TombstoneService,
    VariantRef,
    confirmation_phrase,
    content_hashes,
    content_ids,
    copies,
    derived_rels,
    erase_rows,
    unresolved_bin_items,
    upstream_claims,
    withdraw_upstream_claims,
)

__all__ = [
    "OUT_OF_REACH",
    "PurgeCollateralError",
    "PurgeRefusedError",
    "PurgeReport",
    "PurgeService",
    "PurgeUnknownError",
    "Refused",
]

log = structlog.get_logger(__name__)

# What no purge can reach (EF-BIB-052 (5)): said in every report, in neutral words.
OUT_OF_REACH: Final = (
    "host_backups",
    "kavita_caches_and_bookmarks",
    "copies_made_outside_shelfhold",
    "host_logs",
    "retained_integrated_backups",
)
ALERT_DELETE_UPSTREAM: Final = "unit.delete_in_suwayomi"
OPEN_TICKETS: Final = ("requested", "downloaded")
_ACTION = RESERVED_ACTIONS["purge.now"]
_RW = "rw"


class PurgeRefusedError(PermissionError):
    """The reserved-action rule refused the actor (a key, a stale session, a non-admin)."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(f"purge.now refused: {type(verdict).__name__}")
        self.verdict = verdict


class PurgeUnknownError(LookupError):
    """No such Unit (or Work): nothing to purge."""


class PurgeCollateralError(RuntimeError):
    """A file of the Unit also carries other Units (a volume): erasing it would erase them."""

    def __init__(self, other_units: int) -> None:
        super().__init__(f"the files of this Unit also carry {other_units} other Unit(s)")
        self.other_units = other_units


@dataclass(frozen=True)
class Refused:
    """A copy the purge did not erase, and why. `reason` is a code, never a path or a title."""

    location: str
    reason: str  # read_only_root | path_escape | outside_map | reserved | not_a_file | io_error
    # | mirror_unreadable


@dataclass(frozen=True)
class PurgeReport:
    anchor_id: str
    unit_ids: tuple[str, ...]
    complete: bool
    erased: int = 0
    unreachable_roots: tuple[str, ...] = ()
    pending_roots: tuple[str, ...] = ()  # tombstone lines owed to roots that were away
    refused: tuple[Refused, ...] = ()
    closed_tickets: int = 0
    upstream_copies: int = 0
    alerts: tuple[str, ...] = ()
    out_of_reach: tuple[str, ...] = OUT_OF_REACH


@dataclass
class _Plan:
    anchor: str
    kind: str  # unit | work
    work_id: str
    unit_ids: list[str]
    variants: list[Any]
    units: list[Any]
    items: list[str]
    contents: list[str]
    copies: list[Copy]
    payload: list[ContentHash]
    derived: list[ContentHash]
    bindings: list[Any]
    resources: list[str] = field(default_factory=list)


@dataclass
class _Progress:
    erased: int = 0
    refused: list[Refused] = field(default_factory=list)
    unreachable: set[str] = field(default_factory=set)
    done_intents: list[str] = field(default_factory=list)


_REFUSALS: tuple[tuple[type[FsopsError], str], ...] = (
    (ReadOnlyRootError, "read_only_root"),
    (PathEscapeError, "path_escape"),
    (OutsidePathMapError, "outside_map"),
    (ReservedPathError, "reserved"),
    (NotRegularFileError, "not_a_file"),
)


class PurgeService:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        tombstones: TombstoneService,
        *,
        clock: Clock,
        data_dir: Path,
        bus: EventBus,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._tombstones = tombstones
        self._clock = clock
        self._data_dir = data_dir
        self._bus = bus
        self._journal = IntentJournal(engine, fsops, clock=clock)
        self._locks = ResourceLocks(engine, clock=clock)

    # -- the two commands ---------------------------------------------------------------------

    def purge_unit(
        self,
        unit_id: str,
        *,
        principal: Principal,
        typed: str | None,
        now: datetime,
        job_id: str | None = None,
        fresh_auth_s: int = 900,
    ) -> PurgeReport:
        self._authorize(principal, unit_id, typed, now, fresh_auth_s)
        with self._engine.connect() as connection:
            plan = self._plan_unit(connection, unit_id)
        return self._run(plan, principal, job_id or new_ulid())

    def purge_work(
        self,
        work_id: str,
        *,
        principal: Principal,
        typed: str | None,
        now: datetime,
        job_id: str | None = None,
        fresh_auth_s: int = 900,
    ) -> PurgeReport:
        """Every Unit of the Work, and a `series_binding` tombstone per binding, so that the
        follow cycle ignores the series upstream (M1a: the series is told, not removed from
        Suwayomi). The Work's and Editions' own metadata rows stay until M1b (EF-BIB-053 (4))."""
        self._authorize(principal, work_id, typed, now, fresh_auth_s)
        with self._engine.connect() as connection:
            plan = self._plan_work(connection, work_id)
        return self._run(plan, principal, job_id or new_ulid())

    def _authorize(
        self, principal: Principal, anchor: str, typed: str | None, now: datetime, fresh_s: int
    ) -> None:
        verdict = authorize_reserved(principal, _ACTION, now=now, fresh_auth_s=fresh_s)
        if not isinstance(verdict, Allowed):
            raise PurgeRefusedError(verdict)
        if typed != confirmation_phrase(anchor):  # always typed, irreplaceable or not
            raise ConfirmationRequiredError(anchor)

    # -- the plan: read only ------------------------------------------------------------------

    def _plan_unit(self, connection: Connection, unit_id: str) -> _Plan:
        work = connection.execute(
            sa.select(catalog.edition.c.work_id)
            .select_from(
                catalog.unit.join(
                    catalog.edition, catalog.edition.c.id == catalog.unit.c.edition_id
                )
            )
            .where(catalog.unit.c.id == unit_id)
        ).scalar()
        if work is None:
            raise PurgeUnknownError(unit_id)
        return self._plan(connection, unit_id, "unit", work, [unit_id])

    def _plan_work(self, connection: Connection, work_id: str) -> _Plan:
        if (
            connection.execute(
                sa.select(catalog.work.c.id).where(catalog.work.c.id == work_id)
            ).first()
            is None
        ):
            raise PurgeUnknownError(work_id)
        unit_ids = list(
            connection.execute(
                sa.select(catalog.unit.c.id)
                .select_from(
                    catalog.unit.join(
                        catalog.edition, catalog.edition.c.id == catalog.unit.c.edition_id
                    )
                )
                .where(catalog.edition.c.work_id == work_id)
                .order_by(catalog.unit.c.id)
            ).scalars()
        )
        return self._plan(connection, work_id, "work", work_id, unit_ids)

    def _plan(
        self, connection: Connection, anchor: str, kind: str, work_id: str, unit_ids: list[str]
    ) -> _Plan:
        e, u, v = catalog.edition, catalog.unit, catalog.unit_variant
        units = list(
            connection.execute(
                sa.select(
                    u,
                    *(getattr(e.c, k) for k in EDITION_KEY_COLUMNS if k != "work_id"),
                    e.c.work_id,
                )
                .select_from(u.join(e, e.c.id == u.c.edition_id))
                .where(u.c.id.in_(unit_ids))
            )
        )
        si, b = acquisition.source_item, acquisition.source_binding
        variants = list(
            connection.execute(
                sa.select(v.c.id, v.c.unit_id, v.c.remote_unit_id, si.c.plugin_id, si.c.source_key)
                .select_from(
                    v.join(b, b.c.id == v.c.source_binding_id).join(
                        si, si.c.id == b.c.source_item_id
                    )
                )
                .where(v.c.unit_id.in_(unit_ids))
            )
        )
        bindings = (
            list(
                connection.execute(
                    sa.select(si.c.plugin_id, si.c.source_key, si.c.remote_id)
                    .select_from(b.join(si, si.c.id == b.c.source_item_id))
                    .where(b.c.work_id == work_id)
                )
            )
            if kind == "work"
            else []
        )
        coverage = export.unit_coverage
        items = list(
            connection.execute(
                sa.select(coverage.c.carrier_id).where(
                    coverage.c.carrier_type == "export_item", coverage.c.unit_id.in_(unit_ids)
                )
            ).scalars()
        )
        contents = content_ids(connection, unit_ids)
        self._refuse_collateral(connection, unit_ids, items, contents)
        payload, derived = content_hashes(connection, unit_ids)
        plan = _Plan(
            anchor, kind, work_id, unit_ids, variants, units, sorted(set(items)), contents,
            copies(connection, unit_ids, items),
            [ContentHash(h.by, h.value) for h in payload],
            [ContentHash(h.by, h.value) for h in derived], bindings,
        )  # fmt: skip
        plan.resources = self._resources(connection, work_id, plan.items)
        return plan

    @staticmethod
    def _refuse_collateral(
        connection: Connection,
        unit_ids: Sequence[str],
        items: Sequence[str],
        contents: Sequence[str],
    ) -> None:
        """A file that also covers other Units cannot go for one of them alone."""
        coverage = export.unit_coverage
        others = connection.execute(
            sa.select(sa.func.count(sa.distinct(coverage.c.unit_id))).where(
                sa.or_(
                    sa.and_(
                        coverage.c.carrier_type == "export_item", coverage.c.carrier_id.in_(items)
                    ),
                    sa.and_(
                        coverage.c.carrier_type == "content", coverage.c.carrier_id.in_(contents)
                    ),
                ),
                coverage.c.unit_id.not_in(unit_ids),
            )
        ).scalar_one()
        if others:
            raise PurgeCollateralError(int(others))

    @staticmethod
    def _resources(connection: Connection, work_id: str, items: Sequence[str]) -> list[str]:
        """The Work, then one `path` per destination folder the Unit's files sit in: the same names
        the exporter locks, so the two never write the same folder at once."""
        record = export.export_record
        folders = {
            f"{row.shelf_destination_id}/{row.rel_path.split('/', 1)[0]}"
            for row in connection.execute(
                sa.select(record.c.shelf_destination_id, record.c.rel_path).where(
                    record.c.export_item_id.in_(items), record.c.rel_path.is_not(None)
                )
            )
        }
        return [work_resource(work_id), *(path_resource(f) for f in sorted(folders))]

    # -- the run ------------------------------------------------------------------------------

    def _run(self, plan: _Plan, principal: Principal, job_id: str) -> PurgeReport:
        with self._locks.hold_resources(job_id, plan.resources):
            self._audit(plan, principal)
            roots = self._roots()
            recorded = self._tombstone(plan, roots)  # steps 1 and 2
            progress = _Progress()
            self._erase_copies(plan, roots, progress)  # steps 3 and 4
            self._erase_derived(plan, progress)
            self._clean_mirror(plan, roots, progress)
            return self._close(plan, recorded, progress)  # step 5

    def _roots(self) -> list[TombstoneRoot]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(platform.physical_root.c.root_id, platform.physical_root.c.path_entry)
                .where(platform.physical_root.c.mode == _RW)
                .order_by(platform.physical_root.c.root_id)
            ).all()
        roots = []
        for row in rows:
            try:
                mounted = self._fsops.root_state(row.path_entry) is RootState.OK
            except FsopsError:
                mounted = False  # not a root this process knows: not written, not trusted
            roots.append(TombstoneRoot(row.root_id, row.path_entry, mounted=mounted))
        return roots

    def _audit(self, plan: _Plan, principal: Principal) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(), actor_id=principal.actor_id, action="purge.now",
                    target=f"{plan.kind}:{plan.anchor}", session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )  # fmt: skip

    # -- steps 1 and 2: the tombstones --------------------------------------------------------

    def _tombstone(self, plan: _Plan, roots: list[TombstoneRoot]) -> set[str]:
        """One tombstone per Variant and per place (and per binding for a Work). A value that a
        previous run already recorded is not written twice. Returns the roots still owed lines."""
        reason = "purge_work" if plan.kind == "work" else "purge_unit"
        pending: set[str] = set()
        for values in self._values(plan):
            if self._tombstones.has_values(values):
                continue
            recorded = self._tombstones.record(values, roots, reason=reason)
            pending.update(recorded.pending)
        # a root that is away keeps its line pending even when an earlier run recorded it
        pending.update(r.root_id for r in roots if self._tombstones.has_pending(r.root_id))
        return pending

    def _values(self, plan: _Plan) -> list[VariantRef | PlaceRef | SeriesBindingRef]:
        values: list[VariantRef | PlaceRef | SeriesBindingRef] = []
        for row in plan.variants:
            values.append(
                VariantRef(row.plugin_id, row.source_key, row.remote_unit_id,
                           tuple(plan.payload), tuple(plan.derived))
            )  # fmt: skip
        for unit in plan.units:
            key = {k: str(getattr(unit, k)) for k in EDITION_KEY_COLUMNS}
            values.append(
                PlaceRef.of(key, track=unit.track, number_segment=unit.number_segment,
                            number_raw=unit.number_raw, part=unit.part)
            )  # fmt: skip
        values.extend(
            SeriesBindingRef(b.plugin_id, b.source_key, b.remote_id) for b in plan.bindings
        )
        return values

    # -- steps 3 and 4: intent, then deletion -------------------------------------------------

    def _erase_copies(self, plan: _Plan, roots: list[TombstoneRoot], progress: _Progress) -> None:
        mounted = {r.root_id for r in roots if r.mounted}
        for copy in plan.copies:
            if copy.root_mode != _RW:
                progress.refused.append(Refused(copy.location, "read_only_root"))
            elif copy.root_id not in mounted:
                progress.unreachable.add(copy.root_id)
            else:
                self._erase_one(copy, progress)

    def _local(self, copy: Copy) -> str:
        rel = f"{RECYCLE_DIR}/{copy.rel_path}" if copy.kind == "bin_entry" else copy.rel_path
        return f"{copy.root_path}/{rel}"

    def _open_intent(self, local: str) -> str | None:
        table = coherence.fs_intent
        with self._engine.connect() as connection:
            return connection.execute(
                sa.select(table.c.id)
                .where(table.c.src_path == local, table.c.state.in_(("pending", "applying")))
                .order_by(table.c.created_at)
            ).scalar()

    def _erase_one(self, copy: Copy, progress: _Progress) -> None:
        local = self._local(copy)
        tree = copy.kind == "bin_entry"
        try:
            seen = self._fsops.observe(local, host=tree)
        except (RootBlockedError, TombstonePendingError):
            progress.unreachable.add(copy.root_id)
            return
        except FsopsError as error:
            progress.refused.append(Refused(copy.location, _reason(error)))
            return
        intent = self._open_intent(local)  # a crashed run's: it is finished, not repeated
        if seen is None:
            progress.erased += 1  # already gone: the closing settles its intent
            if intent is not None:
                progress.done_intents.append(intent)
            return
        kind, identity_ = seen
        if kind == "symlink" or (tree and kind != "dir") or (not tree and kind != "file"):
            progress.refused.append(Refused(copy.location, "not_a_file"))
            return
        if intent is None:
            intent = self._intent(local, tree, identity_)
        self._journal.mark_applying(intent)
        try:
            self._erase_copy(local, tree)
        except FileNotFoundError:
            pass  # gone between the look and the delete: the same end
        except (RootBlockedError, TombstonePendingError):
            self._journal.cancel(intent)
            progress.unreachable.add(copy.root_id)
            return
        except FsopsError as error:
            self._journal.cancel(intent)  # nothing was deleted
            progress.refused.append(Refused(copy.location, _reason(error)))
            return
        except OSError:
            progress.refused.append(Refused(copy.location, "io_error"))
            return  # the intent stays open: a half-done deletion is for the rerun
        progress.erased += 1
        progress.done_intents.append(intent)

    def _intent(self, local: str, tree: bool, identity_: FileIdentity) -> str:
        digest = None if tree else self._fsops.sha256_of(local)
        return self._journal.record(
            op="delete" if tree else "unlink", src=local,
            expected_sha256=None if digest is None else _prefixed(digest),
            expected_file_id=None if tree else identity_,
        )  # fmt: skip

    def _erase_copy(self, local: str, tree: bool) -> None:
        if tree:
            self._fsops.purge_remove_tree(local)
        else:
            self._fsops.purge_unlink(local)

    # -- /data and the mirror -----------------------------------------------------------------

    def _erase_derived(self, plan: _Plan, progress: _Progress) -> None:
        for unit_id in plan.unit_ids:
            for rel in derived_rels(unit_id):
                if self._erase_data(rel):
                    progress.erased += 1

    def _erase_data(self, rel: str) -> bool:
        try:
            return erase_data_path(self._data_dir, rel)
        except (OSError, ValueError):
            log.warning("purge.data_not_erased")  # reported by the file system, never the path
            return False

    def _needles(self, plan: _Plan) -> tuple[list[str], list[str]]:
        hashes = [h.value for h in (*plan.payload, *plan.derived)]
        return [*plan.items, *(str(v.id) for v in plan.variants)], hashes

    def _clean_mirror(self, plan: _Plan, roots: list[TombstoneRoot], progress: _Progress) -> None:
        """The lines of the Unit leave the provenance mirror of every mounted root: they hold its
        Variant, its hashes and its export item (never a title)."""
        ids, hashes = self._needles(plan)
        for root in roots:
            if not root.mounted:
                continue
            for name in self._fsops.list_mirror(root.path):
                self._clean_mirror_file(root, name, plan, ids, hashes, progress)

    def _clean_mirror_file(
        self, root: TombstoneRoot, name: str, plan: _Plan, ids: list[str], hashes: list[str],
        progress: _Progress,
    ) -> None:  # fmt: skip
        raw = self._fsops.read_mirror(root.path, name)
        if raw is None:
            return
        text = raw.decode("utf-8", errors="replace")
        mentions = any(n in text for n in (*ids, *hashes))
        try:
            document = MirrorDocument.parse(raw)
            if document.read_only:
                raise MirrorFormatError("newer major")
        except MirrorFormatError:
            if mentions:  # cannot be rewritten, and quotes the Unit: said, not guessed
                progress.refused.append(Refused("mirror", "mirror_unreadable"))
            return
        cleaned, gone = drop_unit_lines(
            document, export_item_ids=plan.items, variant_ids=[str(v.id) for v in plan.variants],
            hashes=hashes,
        )  # fmt: skip
        if not gone:
            return
        data = cleaned.dumps()
        target = f"{root.path}/{_mirror_rel(name)}"
        intent = self._journal.record(op="write", dst=target, expected_sha256=_prefixed_bytes(data))
        self._journal.mark_applying(intent)
        try:
            self._fsops.write_mirror(root.path, name, data)
        except FsopsError:
            self._journal.cancel(intent)
            progress.refused.append(Refused("mirror", "io_error"))
            return
        self._journal.mark_done(intent)

    # -- step 5: the closing ------------------------------------------------------------------

    def _close(self, plan: _Plan, pending: set[str], progress: _Progress) -> PurgeReport:
        complete = not (progress.refused or progress.unreachable or self._unresolved_bin(plan))
        events: list[PublishedEvent] = []
        with self._engine.begin() as connection:
            for intent in progress.done_intents:
                self._journal.mark_done(intent, within=connection)
            closed = self._close_tickets(connection, plan)
            upstream = upstream_claims(connection, plan.unit_ids)
            withdraw_upstream_claims(connection, plan.unit_ids)
            if complete:
                self._erase_rows(connection, plan)
                self._scrub_journals(connection, plan)
            events.append(
                self._bus.publish(
                    connection, "ops.purge_done",
                    {
                        f"{plan.kind}_id": plan.anchor, "erased": progress.erased,
                        "complete": complete, "upstream_copies": upstream,
                    },
                )
            )  # fmt: skip
        self._bus.deliver(events)
        return PurgeReport(
            anchor_id=plan.anchor, unit_ids=tuple(plan.unit_ids), complete=complete,
            erased=progress.erased, unreachable_roots=tuple(sorted(progress.unreachable)),
            pending_roots=tuple(sorted(pending)), refused=tuple(progress.refused),
            closed_tickets=closed, upstream_copies=upstream,
            alerts=(ALERT_DELETE_UPSTREAM,) if upstream else (),
        )  # fmt: skip

    def _unresolved_bin(self, plan: _Plan) -> int:
        with self._engine.connect() as connection:
            return unresolved_bin_items(connection, plan.unit_ids)

    @staticmethod
    def _close_tickets(connection: Connection, plan: _Plan) -> int:
        """Shelfhold's side only (M1a, N5): nothing is asked of the service. Every ticket of these
        Variants ends `closed_by_purge`; the check on the table needs it before the Variant goes."""
        table = acquisition.download_request
        variants = [v.id for v in plan.variants]
        if not variants:
            return 0
        closed = connection.execute(
            sa.select(sa.func.count())
            .select_from(table)
            .where(table.c.variant_id.in_(variants), table.c.state.in_(OPEN_TICKETS))
        ).scalar_one()
        connection.execute(
            sa.update(table)
            .where(table.c.variant_id.in_(variants), table.c.state != "closed_by_purge")
            .values(state="closed_by_purge", updated_at=sa.func.current_timestamp())
        )
        return int(closed)

    def _scrub_journals(self, connection: Connection, plan: _Plan) -> None:
        """The intents and plan operations that are over and name a copy of the Unit: their paths
        spell its title (series folder, file name), and the purge leaves no trace (EF-BIB-053)."""
        paths = {self._local(c) for c in plan.copies}
        if not paths:
            return
        intent, op, plan_table = coherence.fs_intent, coherence.export_op, coherence.export_plan
        connection.execute(
            sa.delete(intent).where(
                intent.c.state.in_(("done", "cancelled", "stale_from_backup")),
                sa.or_(intent.c.src_path.in_(paths), intent.c.dst_path.in_(paths)),
            )
        )
        finished = sa.select(plan_table.c.plan_id).where(
            plan_table.c.status.in_(("done", "cancelled", "failed", "stale"))
        )
        connection.execute(
            sa.delete(op).where(
                op.c.plan_id.in_(finished),
                sa.or_(op.c.src_path.in_(paths), op.c.dst_path.in_(paths)),
            )
        )

    @staticmethod
    def _erase_rows(connection: Connection, plan: _Plan) -> None:
        position = reading.reading_position
        connection.execute(
            sa.delete(position).where(
                sa.or_(
                    sa.and_(
                        position.c.carrier_type == "export_item",
                        position.c.carrier_id.in_(plan.items),
                    ),
                    sa.and_(
                        position.c.carrier_type == "content",
                        position.c.carrier_id.in_(plan.contents),
                    ),
                )
            )
        )
        erase_rows(connection, plan.unit_ids, plan.items)
        connection.execute(
            sa.delete(export.export_item).where(export.export_item.c.id.in_(plan.items))
        )
        v = catalog.unit_variant
        connection.execute(sa.delete(v).where(v.c.unit_id.in_(plan.unit_ids)))
        connection.execute(sa.delete(catalog.unit).where(catalog.unit.c.id.in_(plan.unit_ids)))


def _reason(error: FsopsError) -> str:
    for kind, code in _REFUSALS:
        if isinstance(error, kind):
            return code
    return "io_error"


def _prefixed(digest: str) -> str:
    return digest if digest.startswith("sha256:") else "sha256:" + digest


def _prefixed_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _mirror_rel(name: str) -> str:
    return f"{MIRROR_DIR}/{name}"
