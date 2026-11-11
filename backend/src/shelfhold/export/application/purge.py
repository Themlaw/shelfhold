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
over the bin, "Mettre à l'abri" and the retained-backup rule for the managed copies. Nothing here
carries a title: the audit target and the event hold ULIDs, the tombstone reason is an
enumeration.

M1b (L17) adds, in this order, between the tombstone and the closing:

- **the service**: the download of each Unit is let go of by `release_claim`, through the port
  (`UpstreamPort`, the road of "Mettre à l'abri": an intent that names the request, one erasure at
  most, a deed that was begun is never sent again). With no connected service the claim is
  withdrawn on our side and the admin is told to delete in Suwayomi (M1a);
- **`/store`**: `INDEX.jsonl` is written again without the Units, under an intent and by atomic
  replacement, *before* the copies go: a kill leaves an index that cites files that exist, never a
  dead line (the next window pass would draw the same file);
- the copies, `/data` and the mirror, as before;
- for a Work, the series is taken out of the service's library (`unfollow`);
- the closing erases, besides the Unit rows, the jobs and the events (and their notifications)
  that cite the purge, and for a Work its metadata rows. The report lists the retained integrated
  backups that were taken while the rows existed (read only: a backup is never rewritten).
"""

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine
from structlog.contextvars import bound_contextvars

from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks, path_resource, work_resource
from shelfhold.export.application.upstream_port import (
    ALREADY_GONE,
    CANCELLED,
    KEPT_BY_OTHER_ACCOUNT,
    RELEASED,
    SeriesKey,
    UpstreamPort,
)
from shelfhold.export.domain.physical_roots import RECYCLE_DIR, RootState
from shelfhold.export.domain.provenance_mirror import (
    MIRROR_DIR,
    MirrorDocument,
    MirrorFormatError,
    drop_unit_lines,
)
from shelfhold.identity.public import Principal
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
from shelfhold.infrastructure.logging import elapsed_ms
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
    ContentHash,
    Copy,
    DownloadTicket,
    MetaProjection,
    PlaceRef,
    PurgeAuthorization,
    PurgeRefusedError,
    SeriesBindingRef,
    StoreIndex,
    TombstoneRoot,
    TombstoneService,
    VariantRef,
    content_hashes,
    content_ids,
    copies,
    created_since,
    derived_rels,
    download_tickets,
    erase_history,
    erase_units,
    erase_work_rows,
    request_ids,
    require_purge_allowed,
    title_needles,
    unresolved_bin_items,
    upstream_claims,
    withdraw_upstream_claims,
    work_rel,
)

__all__ = [
    "OUT_OF_REACH",
    "PurgeCollateralError",
    "PurgeRefusedError",
    "PurgeReport",
    "PurgeService",
    "PurgeUnknownError",
    "Refused",
    "Retained",
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
_RW = "rw"


class PurgeUnknownError(LookupError):
    """No such Unit (or Work): nothing to purge."""


class PurgeCollateralError(RuntimeError):
    """A file of the Unit also carries other Units (a volume): erasing it would erase them."""

    def __init__(self, other_units: int) -> None:
        super().__init__(f"the files of this Unit also carry {other_units} other Unit(s)")
        self.other_units = other_units


REMOVE_BY_HAND = (
    "remove the file by hand (in Kavita or on the host: it is a read-only adoption root), "
    "then run `purge --now` again"
)


@dataclass(frozen=True)
class Refused:
    """A copy the purge did not erase, and why. `reason` is a code, never a path or a title."""

    location: str
    reason: str  # read_only_root | path_escape | outside_map | reserved | not_a_file | io_error
    # | mirror_unreadable
    exit: str = ""  # the way out when only the admin can take it (never a path or a title)


@dataclass(frozen=True)
class Retained:
    """An integrated backup that was taken while the rows existed and is still kept: said, never
    touched. A name that is a time, never a title."""

    name: str
    taken_at: datetime


BackupsSince = Callable[[datetime], Sequence[tuple[str, datetime]]]
EXIT_UPSTREAM = (
    "run `intents list`, check the download in the service, `intents resolve`, then run "
    "`purge --now` again"
)


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
    upstream_released: int = 0  # downloads the service was asked to erase (`release_claim`)
    upstream_kept_by_others: int = 0  # `ui_login`: another account keeps the file (measured)
    unfollowed: int = 0  # series taken out of the service's library
    retained_backups: tuple[Retained, ...] = ()
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
    tickets: list[DownloadTicket] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    needles: list[str] = field(default_factory=list)
    since: datetime | None = None
    resources: list[str] = field(default_factory=list)


@dataclass
class _Progress:
    erased: int = 0
    refused: list[Refused] = field(default_factory=list)
    unreachable: set[str] = field(default_factory=set)
    done_intents: list[str] = field(default_factory=list)
    claimed: int = 0  # upstream claims Shelfhold counted before the purge
    released: int = 0
    kept_by_others: int = 0
    unfollowed: int = 0
    unsettled: set[str] = field(default_factory=set)  # requests whose ticket must stay


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
        fresh_auth_s: int,
        store_dir: Path | None = None,
        upstream: UpstreamPort | None = None,
        backups: BackupsSince | None = None,
        index_lock: Callable[[], AbstractContextManager[Any]] | None = None,
    ) -> None:
        self._index_lock = index_lock
        self._fresh_auth_s = fresh_auth_s  # from the settings, never from the caller
        self._store_dir = store_dir
        self._upstream = upstream
        self._backups = backups
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
    ) -> PurgeReport:
        allowed = require_purge_allowed(
            unit_id, principal=principal, now=now, typed=typed, fresh_auth_s=self._fresh_auth_s
        )
        with self._engine.connect() as connection:
            plan = self._plan_unit(connection, unit_id)
        return self._run(plan, allowed, job_id or new_ulid())

    def purge_work(
        self,
        work_id: str,
        *,
        principal: Principal,
        typed: str | None,
        now: datetime,
        job_id: str | None = None,
    ) -> PurgeReport:
        """Every Unit of the Work, and a `series_binding` tombstone per binding, so that the
        follow cycle ignores the series upstream. M1b: the series leaves the service's library
        (`unfollow`) and the Work's metadata rows go (EF-BIB-053 (2), (4))."""
        allowed = require_purge_allowed(
            work_id, principal=principal, now=now, typed=typed, fresh_auth_s=self._fresh_auth_s
        )
        with self._engine.connect() as connection:
            plan = self._plan_work(connection, work_id)
        return self._run(plan, allowed, job_id or new_ulid())

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
        plan.tickets = download_tickets(connection, unit_ids)
        plan.requests = request_ids(connection, unit_ids)
        plan.needles = title_needles(connection, unit_ids, work_id if kind == "work" else None)
        plan.since = created_since(connection, unit_ids, work_id if kind == "work" else None)
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

    def _run(self, plan: _Plan, allowed: PurgeAuthorization, job_id: str) -> PurgeReport:
        report = self._run_locked(plan, allowed, job_id)
        if report.complete:
            self._checkpoint()  # after the locks went: their rows spell the folder (a title)
        return report

    def _run_locked(self, plan: _Plan, allowed: PurgeAuthorization, job_id: str) -> PurgeReport:
        held = plan
        for _ in range(3):
            with self._locks.hold_resources(job_id, held.resources):
                fresh = self._replan(held)  # what arrived while the locks were awaited counts
                if set(fresh.resources) <= set(held.resources):
                    return self._execute(fresh, allowed)
            held = fresh  # a folder the first look did not know: hold it too, look again
        raise PurgeCollateralError(0)  # pragma: no cover (three changes in a row: refuse)

    def _replan(self, plan: _Plan) -> _Plan:
        with self._engine.connect() as connection:
            if plan.kind == "work":
                return self._plan_work(connection, plan.anchor)
            return self._plan_unit(connection, plan.anchor)

    def _execute(self, plan: _Plan, allowed: PurgeAuthorization) -> PurgeReport:
        started = time.perf_counter()
        with bound_contextvars(series_id=plan.work_id):
            report = self._execute_bound(plan, allowed)
        report = self._with_backups(report, plan)
        # counts only: no title, no path and never a tombstone value (they are stored sealed)
        log.info(
            "purge.finished",
            kind=plan.kind,
            anchor_id=plan.anchor,
            units=len(plan.unit_ids),
            erased=report.erased,
            complete=report.complete,
            refused=len(report.refused),
            unreachable_roots=len(report.unreachable_roots),
            pending_roots=len(report.pending_roots),
            duration_ms=elapsed_ms(started),
        )
        return report

    def _execute_bound(self, plan: _Plan, allowed: PurgeAuthorization) -> PurgeReport:
        self._audit(plan, allowed)
        roots = self._roots()
        recorded = self._tombstone(plan, roots)  # steps 1 and 2
        log.debug("purge.tombstoned", pending_roots=len(recorded), roots=len(roots))
        # a root that still owes its line gets no deletion: the line is first, on every root
        roots = [r if not self._tombstones.has_pending(r.root_id) else _away(r) for r in roots]
        progress = _Progress()
        with self._engine.connect() as connection:
            progress.claimed = upstream_claims(connection, plan.unit_ids)
        self._release_upstream(plan, progress)  # the service, before the files (L17)
        self._rewrite_index(plan, progress)  # the index of /store, before its copies go
        self._erase_copies(plan, roots, progress)  # steps 3 and 4
        self._erase_derived(plan, progress)
        self._clean_mirror(plan, roots, progress)
        self._unfollow(plan, progress)
        return self._close(plan, allowed, recorded, progress)  # step 5

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

    def _audit(self, plan: _Plan, allowed: PurgeAuthorization) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(), actor_id=allowed.actor, action="purge.now",
                    target=f"{plan.kind}:{plan.anchor}",
                )
            )  # fmt: skip

    # -- steps 1 and 2: the tombstones --------------------------------------------------------

    def _tombstone(self, plan: _Plan, roots: list[TombstoneRoot]) -> set[str]:
        """One tombstone per Variant and per place (and per binding for a Work). A value that a
        previous run already recorded is not written twice. Returns the roots still owed lines."""
        reason = "purge_work" if plan.kind == "work" else "purge_unit"
        pending: set[str] = set()
        for root in roots:  # an earlier run may have left lines owed (transient error): write them
            if root.mounted:
                self._tombstones.flush_pending(root)
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
                log.debug("purge.copy_decision", decision="read_only", root_id=copy.root_id)
                self._read_only_copy(copy, progress)
            elif copy.root_id not in mounted:
                log.debug("purge.copy_decision", decision="root_away", root_id=copy.root_id)
                progress.unreachable.add(copy.root_id)
            else:
                log.debug("purge.copy_decision", decision="erase", root_id=copy.root_id,
                          kind=copy.kind)  # fmt: skip
                self._erase_one(copy, progress)

    def _read_only_copy(self, copy: Copy, progress: _Progress) -> None:
        """Shelfhold never writes in an adoption root. A file already gone counts as erased (the
        admin removed it); one still there is reported with the way out, and the same command,
        run again after the removal, finishes. It never blocks for good."""
        try:
            gone = self._fsops.observe(self._local(copy)) is None
        except FsopsError:
            gone = False  # cannot look: it is reported, not assumed gone
        if gone:
            progress.erased += 1
        else:
            progress.refused.append(Refused(copy.location, "read_only_root", REMOVE_BY_HAND))

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

    # -- the service (L17, EF-SRC-033) ----------------------------------------------------------

    def _release_upstream(self, plan: _Plan, progress: _Progress) -> None:
        """Let the service go of the downloads of these Units, once each. Nothing is asked when
        no service is connected (M1a: the claim is withdrawn at the closing and the admin is told
        to delete in Suwayomi). A request the service did not settle keeps its ticket, so that
        the same command, run again, reaches it."""
        if not plan.tickets or self._upstream is None:
            return
        for result in self._upstream.release(plan.tickets):
            if result.outcome in (RELEASED, ALREADY_GONE, CANCELLED):
                progress.released += result.outcome == RELEASED
            elif result.outcome == KEPT_BY_OTHER_ACCOUNT:
                progress.kept_by_others += 1  # the claim is withdrawn, the file is not ours
            else:
                progress.unsettled.add(result.request_id)
                code = {"unresolved": "upstream_unresolved"}.get(result.outcome, result.reason)
                exit_ = EXIT_UPSTREAM if result.outcome == "unresolved" else ""
                progress.refused.append(Refused("upstream", code or "service_unavailable", exit_))

    def _unfollow(self, plan: _Plan, progress: _Progress) -> None:
        """A Work: its series leave the service's library, once the chapters have (a series that
        is gone could not be asked about its downloads). Idempotent in the service."""
        if plan.kind != "work" or self._upstream is None or progress.unsettled:
            return
        keys: list[SeriesKey] = sorted(
            {(b.plugin_id, b.source_key, b.remote_id) for b in plan.bindings}
        )
        for result in self._upstream.unfollow(keys):
            if result.done:
                progress.unfollowed += 1
            else:
                progress.refused.append(Refused("upstream", result.reason or "unfollow_failed"))

    def _checkpoint(self) -> None:
        """The write-ahead log holds the pages the purge rewrote, with the rows it erased in their
        earlier state: it is folded into the file and emptied (the pages were blanked by
        `secure_delete`). Best effort: a reader that holds the log keeps it for the next run."""
        with self._engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")

    def _with_backups(self, report: PurgeReport, plan: _Plan) -> PurgeReport:
        """The integrated backups taken while these rows existed, read only (EF-BIB-052 (5))."""
        if self._backups is None or plan.since is None:
            return report
        found = tuple(Retained(name, at) for name, at in self._backups(plan.since))
        return replace(report, retained_backups=found)

    # -- /store: the index (L17, EF-INT-027) ------------------------------------------------------

    def _rewrite_index(self, plan: _Plan, progress: _Progress) -> None:
        """`store/INDEX.jsonl` without these Units, written under an intent by atomic replacement.
        It goes before the copies: a kill leaves an index that cites files that exist (the
        window's own pass draws the same file), never a line that points at nothing."""
        if self._store_dir is None:
            return
        index = StoreIndex(self._engine, self._fsops, str(self._store_dir), self._tombstones)
        # rendered and written under the lock of the index, the one the window's pass takes: a
        # render made before this purge must not be written after it (it would cite the Units
        # again); a lock held past its timeout stops the purge here, before any copy goes, and the
        # same command run again finishes
        with self._index_lock() if self._index_lock is not None else nullcontext():
            self._write_index(index, plan, progress)

    def _write_index(self, index: StoreIndex, plan: _Plan, progress: _Progress) -> None:
        data, _, _ = index.render(without_units=plan.unit_ids)
        try:
            current: bytes | None = self._fsops.read_bytes(index.path)
        except FileNotFoundError:
            current = None
        except OutsidePathMapError:
            return  # /store is not a root of this instance: no index to speak of
        except FsopsError:
            progress.refused.append(Refused("store_index", "root_away"))
            return
        if current == data or (current is None and not data):
            return  # already says exactly this (or never was, and would say nothing)
        intent = self._journal.record(
            op="write", dst=index.path, expected_sha256=_prefixed_bytes(data)
        )
        self._journal.mark_applying(intent)
        try:
            self._fsops.write_atomic(index.path, data, replace=True)
        except (RootBlockedError, TombstonePendingError):
            self._journal.cancel(intent)
            progress.refused.append(Refused("store_index", "root_away"))
            return
        except FsopsError:
            self._journal.cancel(intent)
            progress.refused.append(Refused("store_index", "io_error"))
            return
        self._journal.mark_done(intent)

    # -- /data and the mirror -----------------------------------------------------------------

    def _erase_derived(self, plan: _Plan, progress: _Progress) -> None:
        for unit_id in plan.unit_ids:
            for rel in derived_rels(unit_id):
                if self._erase_data(rel):
                    progress.erased += 1
        if plan.kind == "work" and self._erase_data(work_rel(plan.work_id)):
            progress.erased += 1  # its `work.json` spells the title: gone before the rows are

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

    def _close(
        self, plan: _Plan, allowed: PurgeAuthorization, pending: set[str], progress: _Progress
    ) -> PurgeReport:
        complete = not (progress.refused or progress.unreachable or self._unresolved_bin(plan))
        events: list[PublishedEvent] = []
        with self._engine.begin() as connection:
            for intent in progress.done_intents:
                self._journal.mark_done(intent, within=connection)
            closed = self._close_tickets(connection, plan, progress.unsettled)
            upstream = max(progress.claimed, upstream_claims(connection, plan.unit_ids))
            withdraw_upstream_claims(connection, plan.unit_ids)
            if complete:
                self._erase_rows(connection, plan, allowed)
                self._scrub_journals(connection, plan)
                self._erase_metadata(connection, plan)
            events.append(
                self._bus.publish(
                    connection, "ops.purge_done",
                    {
                        f"{plan.kind}_id": plan.anchor, "erased": progress.erased,
                        "complete": complete, "upstream_copies": upstream,
                    },
                )
            )  # fmt: skip
        if complete:
            self._project_work(plan.work_id)  # its work.json no longer lists the purged Units
        self._bus.deliver(events)
        return PurgeReport(
            anchor_id=plan.anchor, unit_ids=tuple(plan.unit_ids), complete=complete,
            erased=progress.erased, unreachable_roots=tuple(sorted(progress.unreachable)),
            pending_roots=tuple(sorted(pending)), refused=tuple(progress.refused),
            closed_tickets=closed, upstream_copies=upstream,
            upstream_released=progress.released, upstream_kept_by_others=progress.kept_by_others,
            unfollowed=progress.unfollowed,
            alerts=(
                (ALERT_DELETE_UPSTREAM,) if self._delete_by_hand(plan, progress, upstream) else ()
            ),
        )  # fmt: skip

    def _project_work(self, work_id: str) -> None:
        """After the commit: `work.json` is written again from the database, or erased when the
        Work is gone (a purged Unit's identifier must not outlive it in a projection)."""
        try:
            outcome = MetaProjection(self._engine, self._data_dir).project_work(work_id)
            if outcome == "missing_unit":
                self._erase_data(work_rel(work_id))
        except (OSError, ValueError):
            log.warning("purge.work_json_not_written")

    def _unresolved_bin(self, plan: _Plan) -> int:
        with self._engine.connect() as connection:
            return unresolved_bin_items(connection, plan.unit_ids)

    def _delete_by_hand(self, plan: _Plan, progress: _Progress, upstream: int) -> bool:
        """The admin is told to delete in Suwayomi when something the purge counted upstream is
        still there: no service in reach (M1a), a file another account keeps, a service that did
        not settle, or a claim with no ticket to ask about."""
        if not upstream:
            return False
        return bool(
            self._upstream is None
            or not plan.tickets
            or progress.kept_by_others
            or progress.unsettled
        )

    @staticmethod
    def _close_tickets(connection: Connection, plan: _Plan, keep: set[str]) -> int:
        """Every ticket of these Variants ends `closed_by_purge`; the check on the table needs it
        before the Variant goes. A request the service did not settle is left as it is: its
        ticket is what the same command, run again, asks about (M1b)."""
        table = acquisition.download_request
        variants = [v.id for v in plan.variants]
        if not variants:
            return 0
        mine = table.c.variant_id.in_(variants), table.c.id.not_in(keep)
        closed = connection.execute(
            sa.select(sa.func.count())
            .select_from(table)
            .where(*mine, table.c.state.in_(OPEN_TICKETS))
        ).scalar_one()
        connection.execute(
            sa.update(table)
            .where(*mine, table.c.state != "closed_by_purge")
            .values(state="closed_by_purge", updated_at=sa.func.current_timestamp())
        )
        return int(closed)

    def _erase_metadata(self, connection: Connection, plan: _Plan) -> None:
        """What only the rows say (EF-BIB-053 (4)): the jobs and the events that cite the purge,
        by identifier or by a title in their text, and for a Work the Work itself."""
        ids = {
            *plan.unit_ids, *plan.items, *plan.contents, *plan.requests,
            *(str(v.id) for v in plan.variants),
        }  # fmt: skip
        if plan.kind == "work":
            ids.add(plan.work_id)
            ids.update(u.edition_id for u in plan.units)
            ids.update(b.id for b in plan.bindings if hasattr(b, "id"))
        jobs, events = erase_history(connection, ids, plan.needles)
        log.debug("purge.history_erased", jobs=jobs, events=events)
        if plan.kind == "work":
            erase_work_rows(connection, plan.work_id)

    def _scrub_journals(self, connection: Connection, plan: _Plan) -> None:
        """The intents and plan operations that are over and name a copy of the Unit: their paths
        spell its title (series folder, file name), and the purge leaves no trace (EF-BIB-053)."""
        paths = {self._local(c) for c in plan.copies}
        if not paths and not plan.needles:
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
        # a finished plan keeps its batches as JSON, with the same paths: it is bookkeeping that
        # is over, and it goes whole (the ones that still wait for their run are not touched)
        wanted = [*paths, *plan.needles]
        for row in connection.execute(
            sa.select(plan_table.c.plan_id, plan_table.c.batches).where(
                plan_table.c.status.in_(("done", "cancelled", "failed", "stale"))
            )
        ).all():
            text = json.dumps(row.batches, ensure_ascii=False)
            if any(n in text or n in text.lower() for n in wanted):
                connection.execute(sa.delete(op).where(op.c.plan_id == row.plan_id))
                connection.execute(sa.delete(plan_table).where(plan_table.c.plan_id == row.plan_id))

    @staticmethod
    def _erase_rows(connection: Connection, plan: _Plan, allowed: PurgeAuthorization) -> None:
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
        connection.execute(
            sa.delete(export.export_item).where(export.export_item.c.id.in_(plan.items))
        )
        erase_units(connection, plan.unit_ids, plan.items, authorization=allowed)


def _away(root: TombstoneRoot) -> TombstoneRoot:
    return TombstoneRoot(root.root_id, root.path, mounted=False, predecessor_root_ids=())


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
