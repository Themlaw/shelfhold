"""The export pipeline: plan, store, apply (EF-EXP-007, EF-EXP-008, EF-EXP-058).

`preview` (the dry run) and `run` (plan then apply) go through the same `_compute`, hence the same
`plan_destination`. A preview is stored as a versioned plan (`PlanStore`, 412 when stale); applying
it replays the stored operations, each under the resource locks of its Work and series folder, the
manifest being committed right after the atomic write. The archive is rendered (pages as received,
ComicInfo last) into the tree of the destination; a `pull` destination only gets a manifest line,
nothing is written.
"""

import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine
from structlog.contextvars import bound_contextvars

from shelfhold.events.bus import EventBus
from shelfhold.export.application import blocked_memo, waiting_marks
from shelfhold.export.application.export_items import create_export_item, ensure_export_series
from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.locks import ResourceLocks, library_resource
from shelfhold.export.application.manifest_write import complete_write
from shelfhold.export.application.mirror import ProvenanceMirror
from shelfhold.export.application.naming_preview import NamingItem
from shelfhold.export.application.plans import PlanOp, PlanStore, StalePlanError
from shelfhold.export.application.ranged_export import (
    PreparedRange,
    RangedExporter,
    RangedExportError,
)
from shelfhold.export.application.rename_plan import (
    PLAN_KIND as RENAME_KIND,
)
from shelfhold.export.application.rename_plan import (
    ItemMove,
    ReaderCounter,
    RenameBook,
    RenameConfirmationRequiredError,
    RenamePreview,
)
from shelfhold.export.application.rendering import (
    CbzRenderer,
    EpubRenderer,
    RenderedCbz,
    RenderedEpub,
    comicinfo_xml,
    epub_opf_edit,
    volume_opf_edit,
)
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.export.application.series_age import due_ages, verified_age
from shelfhold.export.application.snapshot import (
    DestinationContext,
    LoadedRequest,
    RangedHash,
    barrier_holds,
    edition_age,
    inputs_version_of,
    load_context,
    load_request,
    paused_series,
    seeded_series,
    version_of,
)
from shelfhold.export.application.target_state import FieldRegistry, ensure_active
from shelfhold.export.application.upgrade import SamePathUpgrade, UpgradeOutcome
from shelfhold.export.domain.field_owner import AGE_RATING
from shelfhold.export.domain.naming import FrozenAttrs, NamingTemplate, Parsed
from shelfhold.export.domain.planner import (
    GATE_CLOSED,
    GONE_STATES,
    AgeGateFacts,
    Blocked,
    Plan,
    PlannedOp,
    PlanRequest,
    PlanWarning,
    SeriesRef,
    age_rank,
    plan_destination,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import read_transaction, write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.logging import debug_enabled, elapsed_ms
from shelfhold.infrastructure.schema import coherence, export
from shelfhold.library.public import (
    ExportSource,
    OriginalNotServableError,
    RootLocator,
    ServableEpub,
    export_sources,
    has_epub,
    keep_origin_opf,
    require_servable_derivative,
    servable_epub,
)
from shelfhold.sdk.age import AgeRating

__all__ = [
    "ApplyReport",
    "ExportPipeline",
    "PlanPreview",
    "SourceMissingError",
    "UnknownPlanError",
    "UnverifiedParseError",
    "decode_op",
    "encode_op",
    "ensure_applicable",
    "wanted_names",
]

log = structlog.get_logger(__name__)
PLAN_KIND = "export"
LOCK_WAIT_SECONDS = 30.0
RUN_ATTEMPTS = 3  # plans made, then found stale at the claim, before the run gives up


class SourceMissingError(Exception):
    """The received archive of a planned Unit is gone: the plan is out of date."""

    code = "export.source_missing"


class _Unread:
    """A placeholder answer while the names are only being collected."""

    min_chapter = 0.0
    min_volume = 0.0


class UnverifiedParseError(Exception):
    """No `plugin/parse` answers behind this pipeline: it can plan, never write (EF-EXP-045)."""

    code = "target.route_missing"


class DestinationPermissionError(PermissionError):
    """The destination refused the write of the exported file itself (EACCES, EROFS...): only
    this waits for `target recheck`. A `PermissionError` anywhere else (the received archive,
    the staging area) is an ordinary failure and is not blamed on the folder."""


class UnknownPlanError(LookupError):
    """No stored plan has this identifier."""


def ensure_applicable(engine: Engine, plan_id: str, *, if_match: str) -> str:
    """The checks of the 412 that need no disk and no Kavita, made when the application of a plan
    is asked for (before a job exists): the plan is known, still `planned`, the validator is the
    one of its preview and nothing moved since. Returns the destination of the plan. The claim and
    the fingerprint of the files stay with `PlanStore.apply`, which runs again in the job."""
    with engine.connect() as connection:
        plan = connection.execute(
            sa.select(coherence.export_plan).where(coherence.export_plan.c.plan_id == plan_id)
        ).one_or_none()
        if plan is None or plan.shelf_destination_id is None:
            raise UnknownPlanError(plan_id)
        ensure_active(connection, plan.shelf_destination_id)  # reinstalled Kavita, child exposure
        ctx = load_context(connection, plan.shelf_destination_id)
        if plan.kind == RENAME_KIND and plan.status == "applying" and if_match == plan.base_version:
            return str(plan.shelf_destination_id)  # a rename plan cut short goes on (EF-EXP-041)
        if (
            plan.status != "planned"
            or if_match != plan.base_version
            or version_of(connection, ctx) != plan.base_version
        ):
            raise StalePlanError("the preview changed: recompute it")
    return str(plan.shelf_destination_id)


class SwitchoverCheck(Protocol):
    def require_clear(self, destination_id: str) -> None: ...


class Preflight(Protocol):
    """The permission preflight (EF-EXP-054): the root before any export, a refused write after."""

    def require_root(self, destination_id: str) -> None: ...
    def mark_denied(self, destination_id: str, export_series_id: str) -> bool: ...


class WriteSessionOpener(Protocol):
    def open(self, *paths: str, guard: Callable[[], None] | None = None) -> Any: ...


@dataclass(frozen=True)
class PlanPreview:
    plan_id: str
    base_version: str
    ops: tuple[PlannedOp, ...]
    blocked: tuple[Blocked, ...]
    warnings: tuple[PlanWarning, ...]
    checks: tuple[str, ...]


@dataclass(frozen=True)
class ApplyReport:
    plan_id: str | None
    ops: tuple[PlannedOp, ...]


def encode_op(op: PlannedOp) -> dict[str, Any]:
    return asdict(op)


def decode_op(payload: dict[str, Any]) -> PlannedOp:
    series = payload["series"]
    return PlannedOp(
        kind=payload["kind"], unit_id=payload["unit_id"], work_id=payload["work_id"],
        rel_path=payload["rel_path"], series=SeriesRef(**series), frozen=payload["frozen"],
        content_hash=payload["content_hash"], export_item_id=payload["export_item_id"],
    )  # fmt: skip


def mark_name_collisions(
    engine: Engine, destination_id: str, request: PlanRequest, plan: Plan
) -> None:
    """A plan blocked by a series name (EF-EXP-032) waits for the administrator: written down
    for `status`, which reads the database from another process. The mark goes with the
    collision (`edition set-series-name` or a change of the library leaves it)."""
    edition_of = {u.unit_id: u.edition_id for u in request.units}
    edition_of |= {v.export_item_id: v.edition_id for v in request.volumes}
    colliding: dict[str, str] = {}
    for blocked in plan.blocked:
        who = blocked.unit_id or blocked.export_item_id
        if blocked.code == "export.series_name_collision" and who in edition_of:
            colliding.setdefault(edition_of[who], blocked.detail)
    reasons = {
        edition_id: f"another series is already called {name!r} in this library"
        for edition_id, name in colliding.items()
    }
    with read_transaction(engine) as connection:  # the marks are already right: no write lock
        if waiting_marks.scope_is_exactly(
            connection, "series_name_collision", destination_id, reasons
        ):
            return
    with write_transaction(engine) as connection:  # `clear_except` and `put` read under the lock
        waiting_marks.clear_except(connection, "series_name_collision", destination_id, colliding)
        for edition_id, reason in reasons.items():
            waiting_marks.put(
                connection, "series_name_collision", edition_id, reason,
                destination_id=destination_id,
            )  # fmt: skip


def wanted_names(
    engine: Engine,
    destination_id: str,
    *,
    segment_policy_confirmed: bool = False,
    ranged_hash: RangedHash = lambda _item: None,
) -> tuple[list[str], str | None]:
    """The names a plan of this destination asks `plugin/parse` about, and the Kavita library
    type to ask with. Read from the database alone: no disk is looked at, so an empty answer
    (nothing to write) costs the disks nothing (ENF-OPS-014). `ranged_hash` reads the kept file of a
    volume under /data only (never a library); the job passes it, or a volume asks no name."""
    asked: list[str] = []

    def record(name: str) -> Parsed:
        asked.append(name)
        return _Unread()

    with engine.connect() as connection:
        ctx = load_context(connection, destination_id)
        loaded = load_request(
            connection, ctx,
            exists=lambda _path: False,  # whether a name is taken is a question for the disk
            covered_locals=open_intent_paths(connection),
            segment_policy_confirmed=segment_policy_confirmed,
            ranged_hash=ranged_hash,
            # the gate is read from Kavita only once a name is asked: a closed one here would plan
            # no EPUB of a rated Work, ask for no name, and the gate would never be read (M2 L32)
            age_gate=AgeGateFacts(open=True),
        )  # fmt: skip
        kind = connection.execute(
            sa.select(export.shelf_destination.c.kavita_library_type).where(
                export.shelf_destination.c.id == destination_id
            )
        ).scalar()
    if blocked_memo.known(engine, destination_id, loaded.version):
        return [], kind  # the disk already said "taken" for these inputs: nothing to ask or write
    mark_name_collisions(
        engine, destination_id, loaded.request, plan_destination(loaded.request, record)
    )
    return sorted(set(asked)), kind


class FamilyBarrierClosedError(Exception):
    """The shelf below 18+ does not take this Edition (any more): nothing is written."""


class _Refused:
    """What `_execute` returns for an operation a last look refused: nothing was written, and
    the report does not list it."""


REFUSED: Final = _Refused()


class SeriesAgeGateClosedError(Exception):
    """The age of the EPUB series is not read back locked: nothing more of it is written."""


class ExportPipeline:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        *,
        locate: RootLocator,
        data_dir: Path,
        clock: Clock,
        parse: Callable[[str], Parsed],
        request_scan: Callable[[str], None] | None = None,
        sessions: WriteSessionOpener | None = None,
        parse_verified: bool,
        segment_policy_confirmed: bool = False,
        renderer: CbzRenderer | None = None,
        mirror: ProvenanceMirror | None = None,
        detector: Callable[[str], object] | None = None,
        gate: Callable[[], None] | None = None,
        operating_bus: EventBus | None = None,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._locate = locate
        self._clock = clock
        self._parse = parse
        self._request_scan = request_scan
        self._sessions = sessions
        self._segment_policy_confirmed = segment_policy_confirmed
        self._parse_verified = parse_verified
        self._data_dir = data_dir
        self._staging = data_dir / "tmp"
        self._renderer = renderer or CbzRenderer(fsops, staging_dir=self._staging)
        self._epub_renderer = EpubRenderer(fsops, staging_dir=self._staging)
        # a volume is read from our own /data (the derivative of the neutralizer), never a library
        self._ranged = RangedExporter(engine, data_dir, staging_dir=self._staging)
        self._ranged_renderer = EpubRenderer(None, staging_dir=self._staging)
        self._operating_bus = operating_bus
        self._age_gate: AgeGateFacts = GATE_CLOSED  # bound per run by the export job
        self._age_reader: Callable[[int], tuple[int, bool] | None] | None = None
        self._tier_reader: Callable[[], int | None] | None = None
        self._locks = ResourceLocks(engine, clock=clock)
        self.mirror = mirror or ProvenanceMirror(
            engine, fsops, clock=clock, operating_bus=operating_bus
        )
        self._detector = detector  # EF-EXP-043: looked at before every export
        self._gate = gate  # raises while the instance is `restored_behind` (EF-INT-026)
        self._preflight: Preflight | None = None
        self._switchover: SwitchoverCheck | None = None
        self._renames = RenameBook(
            engine, fsops, locate=locate, clock=clock, locks=self._locks, request_scan=request_scan
        )

    def bind_readers(self, counter: ReaderCounter | None) -> None:
        """Who reads a folder in Kavita, for the warning of a rename (EF-EXP-037)."""
        self._renames.bind_readers(counter)

    def bind_scan_drain(self, drain: Callable[[], None] | None) -> None:
        """What sends the scans asked so far: a rename plan calls it at the end of each batch."""
        self._renames.bind_scan_drain(drain)

    def rename_preview(
        self,
        destination_id: str,
        *,
        template: NamingTemplate | None = None,
        moves: Sequence[ItemMove] = (),
    ) -> RenamePreview:
        """The plan of renames a new template, a reshelving or a binding migration makes: the
        files already exported are renamed, never deleted and written again (EF-EXP-030)."""
        self._require_writable()
        return self._renames.preview(destination_id, template=template, moves=moves)

    def kind_of(self, plan_id: str) -> str:
        with self._engine.connect() as connection:
            kind = connection.execute(
                sa.select(coherence.export_plan.c.kind).where(
                    coherence.export_plan.c.plan_id == plan_id
                )
            ).scalar()
        if kind is None:
            raise UnknownPlanError(plan_id)
        return str(kind)

    def require_confirmation(self, plan_id: str, *, confirmed: bool) -> None:
        """The refusals of a rename plan that need no disk write, asked before a job exists. A
        confirmation that passes is kept with the plan: the job relies on it (EF-EXP-037)."""
        if self.kind_of(plan_id) == RENAME_KIND:
            try:
                self._renames.require_confirmation(plan_id, confirmed=confirmed)
            except RenameConfirmationRequiredError as error:
                # the answer tells the admin this count: confirming it is what comes next
                self._renames.announce(plan_id, error.readers, exact=error.exact)
                raise
            if confirmed:
                self._renames.record_confirmation(plan_id)

    def require_not_applying(self, plan_id: str) -> None:
        """409 `plan.applying`: a job is applying this plan now; a second request leaves it be."""
        if self.kind_of(plan_id) == RENAME_KIND and self._renames.is_applying(plan_id):
            raise StalePlanError("the plan is being applied", busy=True)

    def _versions(self, ctx: DestinationContext) -> tuple[Callable[[], str], Callable[[], str]]:
        def current() -> str:
            with self._engine.connect() as connection:
                return version_of(connection, ctx)

        def inputs() -> str:
            with self._engine.connect() as connection:
                return inputs_version_of(connection, ctx)

        return current, inputs

    def _settle_renames(self, destination_id: str) -> int:
        """The intents a stop left open for the steps of rename plans of this destination."""
        journal = IntentJournal(self._engine, self._fsops, clock=self._clock)
        hook = self._renames.settle_hook()
        settled = 0
        for intent in journal.open_intents():
            ours = intent.op == "rename" and (
                intent.payload.get("destination_id") == destination_id
                and intent.payload.get("scope") in ("file", "series_dir")
            )
            report = journal.resolve_one(intent.id, on_done=hook) if ours else None
            if report is not None:
                settled += len(report.replayed) + len(report.completed)
        return settled

    def _apply_rename(
        self, plan_id: str, *, if_match: str, job_id: str, confirm_progress_loss: bool
    ) -> ApplyReport:
        with self._engine.connect() as connection:
            plan = connection.execute(
                sa.select(coherence.export_plan).where(coherence.export_plan.c.plan_id == plan_id)
            ).one()
            ensure_active(connection, plan.shelf_destination_id)
            ctx = load_context(connection, plan.shelf_destination_id)
        versions = self._versions(ctx)
        if plan.status == "applying" and if_match == plan.base_version:
            self._renames.resume(
                plan_id, job_id=job_id, versions=versions,
                settle=lambda: self._settle_renames(ctx.destination_id),
            )  # fmt: skip
        else:
            self._renames.apply(
                plan_id, if_match=if_match, job_id=job_id, versions=versions,
                confirm_progress_loss=confirm_progress_loss,
            )  # fmt: skip
        self.heal_mirror(ctx.destination_id)
        return ApplyReport(plan_id, ())

    def use_switchover(self, gate: SwitchoverCheck) -> None:
        """Refuse to export a shelf fed directly by Suwayomi until the procedure is confirmed
        (EF-EXP-044)."""
        self._switchover = gate

    def use_preflight(self, preflight: Preflight) -> None:
        """Probe the root of a destination before any export, and park a series whose write is
        refused (EF-EXP-054): the composition root sets it once."""
        self._preflight = preflight

    def _require_writable(self) -> None:
        if self._gate is not None:
            self._gate()
        elif self._fsops.frozen:
            raise RestoredBehindError("restored_behind: no export until the adoption")

    def _require_verified(self) -> None:
        if not self._parse_verified:
            raise UnverifiedParseError("names are not checked by plugin/parse: preview only")

    # -- planning --------------------------------------------------------------------------------

    def bind_parse(self, parse: Callable[[str], Parsed], *, verified: bool) -> None:
        """Put the `plugin/parse` answers (read in bulk beforehand) behind the planner."""
        self._parse = parse
        self._parse_verified = verified

    def bind_age_gate(
        self,
        facts: AgeGateFacts,
        reader: Callable[[int], tuple[int, bool] | None] | None = None,
        tier: Callable[[], int | None] | None = None,
    ) -> None:
        """Put what Kavita said of the series ages (read just before) behind the planner and the
        last look before an EPUB is written (EF-EXP-040). Both readers ask Kavita now, under the
        locks, right before the write: `reader` gives `(age_rating, locked)` of a Kavita series
        (None: Kavita no longer shows it), `tier` the lowest tier of a restricted account that
        sees the "unknown" series of the shelf. Unbound, the gate is closed."""
        self._age_gate = facts
        self._age_reader = reader
        self._tier_reader = tier

    def wanted_names(self, destination_id: str) -> tuple[list[str], str | None]:
        """See the module function: fetched in bulk, then `bind_parse` makes the plan applicable."""
        return wanted_names(
            self._engine,
            destination_id,
            segment_policy_confirmed=self._segment_policy_confirmed,
            ranged_hash=self._ranged.source_hash,
        )

    def naming_inputs(self, destination_id: str) -> tuple[list[NamingItem], list[str], str | None]:
        """What a naming preview renders: the Units the plan would write, the paths already on
        the shelf, and the Kavita library type. From the database alone: no disk, no write."""
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            loaded = load_request(
                connection, ctx,
                exists=lambda _path: False,
                covered_locals=open_intent_paths(connection),
                segment_policy_confirmed=self._segment_policy_confirmed,
                age_gate=self._age_gate,
                ranged_hash=self._ranged.source_hash,
            )  # fmt: skip
            kind = connection.execute(
                sa.select(export.shelf_destination.c.kavita_library_type).where(
                    export.shelf_destination.c.id == destination_id
                )
            ).scalar()
        plan = plan_destination(loaded.request, None)
        items = [
            NamingItem(
                op.unit_id or op.export_item_id or "",
                FrozenAttrs.stored(op.frozen),
                PurePosixPath(op.rel_path).suffix,
            )
            for op in plan.ops
            if op.rel_path is not None
        ]
        existing = [r.rel_path for r in loaded.request.records if r.rel_path]
        return items, existing, kind

    def _compute(
        self, destination_id: str, *, disk: bool = True
    ) -> tuple[DestinationContext, LoadedRequest, Plan]:
        """The one way to a plan: the dry run and the application both come through here.
        `disk=False` plans from the database alone: no name is checked against a disk."""
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            covered = open_intent_paths(connection)
            loaded = load_request(
                connection, ctx,
                exists=(lambda path: self._fsops.observe(path) is not None)
                if disk
                else (lambda _path: False),
                covered_locals=covered,
                segment_policy_confirmed=self._segment_policy_confirmed,
                age_gate=self._age_gate,
                ranged_hash=self._ranged.source_hash,
            )  # fmt: skip
        plan = plan_destination(loaded.request, self._parse)
        mark_name_collisions(self._engine, destination_id, loaded.request, plan)
        return ctx, loaded, plan

    def _store(self, ctx: DestinationContext, loaded: LoadedRequest, plan: Plan) -> str:
        files = [ctx.local(op.rel_path) for op in plan.ops if op.rel_path is not None]
        store = PlanStore(self._engine, clock=self._clock, current_version=lambda: loaded.version)
        record = store.create(
            kind=PLAN_KIND,
            files=files,
            ops=[
                PlanOp(
                    op.kind, dst=ctx.local(op.rel_path) if op.rel_path else None,
                    payload=encode_op(op),
                )
                for op in plan.ops
            ],
            shelf_destination_id=ctx.destination_id,
        )  # fmt: skip
        return record.plan_id

    def preview(self, destination_id: str) -> PlanPreview:
        """The dry run: the plan, stored so that it can be applied once, nothing written."""
        ctx, loaded, plan = self._compute(destination_id)
        plan_id = self._store(ctx, loaded, plan)
        return PlanPreview(
            plan_id, loaded.version, plan.ops, plan.blocked, plan.warnings, plan.checks
        )

    def run(self, destination_id: str, *, job_id: str) -> ApplyReport:
        """Plan, then apply; a plan with nothing to do is not even stored (idempotence).

        The first plan is made from the database alone: while it is empty no disk is read or
        written, no lease is taken and no sentinel is looked at (ENF-OPS-014, EF-EXP-029). Only a
        plan with something to write goes on to the checks that need the disks, and is then made
        again with what they show."""
        self._require_writable()
        self._require_verified()
        with self._engine.connect() as connection:
            ensure_active(connection, destination_id)
        _ctx, wanted, first = self._compute(destination_id, disk=False)
        if not first.ops:
            log.debug("export.nothing_to_write", destination_id=destination_id)
            return ApplyReport(None, ())
        if blocked_memo.known(self._engine, destination_id, wanted.version):
            log.debug("export.blocked_memo", destination_id=destination_id)
            return ApplyReport(None, ())  # the disk said "taken" for these inputs: ask no more
        if self._detector is not None:
            self._detector(destination_id)
        if self._switchover is not None:
            self._switchover.require_clear(destination_id)
        if self._preflight is not None:
            with self._root_session(destination_id):  # the probe writes: under the lease too
                self._preflight.require_root(destination_id)
        for attempt in range(RUN_ATTEMPTS):
            ctx, loaded, plan = self._compute(destination_id)
            self._heal_mirror(ctx)
            if not plan.ops:
                # only what the disk alone decides: a name Kavita misreads depends on Kavita,
                # which can change its mind with no change of ours
                if plan.blocked and all(
                    b.code == "export.destination_occupied" for b in plan.blocked
                ):
                    blocked_memo.remember(self._engine, destination_id, loaded.version)
                log.debug("export.plan_empty", blocked=[b.code for b in plan.blocked])
                return ApplyReport(None, ())
            blocked_memo.forget(self._engine, destination_id)
            plan_id = self._store(ctx, loaded, plan)
            self._log_plan(plan_id, plan)
            try:
                return self.apply(plan_id, if_match=loaded.version, job_id=job_id)
            except StalePlanError as error:
                # another export of the destination moved the manifest between this plan and its
                # claim: start again from what is there now (nothing left to do ends the run)
                if attempt == RUN_ATTEMPTS - 1:
                    raise StalePlanError(  # not the "preview" of a person: nobody asked for one
                        "other exports kept changing the destination while this export planned"
                    ) from error
                log.info("export.plan_stale_retry", destination_id=destination_id)
        raise AssertionError("unreachable")  # pragma: no cover

    def _root_session(self, destination_id: str) -> AbstractContextManager[object]:
        if self._sessions is None:
            return nullcontext()
        with self._engine.connect() as connection:
            root = load_context(connection, destination_id).root_local
        return self._sessions.open(root) if root else nullcontext()

    def heal_mirror(self, destination_id: str) -> None:
        """The mirror of a destination projected again if a file is lost or out of date. A job of
        the maintenance window, since looking for a lost file reads the disk (ENF-OPS-014): a run
        with nothing to write does not, a run with something to write heals it on its way."""
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
        self._heal_mirror(ctx)

    def _heal_mirror(self, ctx: DestinationContext) -> None:
        """A mirror file lost, or never written because a run stopped after its file: the
        database is the authority, so it is projected again (EF-EXP-063)."""
        try:
            if not self.mirror.would_write(ctx.destination_id):
                return
            opened = (
                self._sessions.open(ctx.local("."))
                if self._sessions and ctx.root_local
                else nullcontext(self._fsops)
            )
            with opened as writer:
                self.mirror.sync_destination(ctx.destination_id, writer=writer)
        except Exception:  # a mirror never stops an export; the next run tries again
            log.warning("mirror_heal_failed", destination=ctx.destination_id)

    # -- applying --------------------------------------------------------------------------------

    @staticmethod
    def _log_plan(plan_id: str, plan: Plan) -> None:
        """INFO: the count per kind. DEBUG: every operation (the path is a title: DEBUG only)."""
        by_kind: dict[str, int] = {}
        for op in plan.ops:
            by_kind[op.kind] = by_kind.get(op.kind, 0) + 1
        log.info(
            "export.plan_computed",
            plan_id=plan_id,
            ops=len(plan.ops),
            blocked=len(plan.blocked),
            warnings=len(plan.warnings),
            **{f"ops_{kind}": n for kind, n in by_kind.items()},
        )
        if debug_enabled():
            for op in plan.ops:
                log.debug(
                    "export.plan_op",
                    plan_id=plan_id,
                    kind=op.kind,
                    unit_id=op.unit_id,
                    series_id=op.work_id,
                )  # ULIDs only: the relative path holds the title (EF-BIB-053)
            for blocked in plan.blocked:
                log.debug("export.plan_blocked", code=blocked.code, unit_id=blocked.unit_id)

    def apply(
        self, plan_id: str, *, if_match: str, job_id: str, confirm_progress_loss: bool = False
    ) -> ApplyReport:
        """Apply a stored plan once. `StalePlanError` (412) when anything moved since."""
        started = time.perf_counter()
        self._require_writable()
        if self.kind_of(plan_id) == RENAME_KIND:  # names are not parsed: they exist already
            return self._apply_rename(
                plan_id, if_match=if_match, job_id=job_id,
                confirm_progress_loss=confirm_progress_loss,
            )  # fmt: skip
        self._require_verified()
        with self._engine.connect() as connection:
            destination_id = connection.execute(
                sa.select(coherence.export_plan.c.shelf_destination_id).where(
                    coherence.export_plan.c.plan_id == plan_id
                )
            ).scalar_one()
            ensure_active(connection, destination_id)  # a reinstalled Kavita, child exposure...
            ctx = load_context(connection, destination_id)

        def current() -> str:
            with self._engine.connect() as connection:
                return version_of(connection, ctx)

        def inputs() -> str:
            with self._engine.connect() as connection:
                return inputs_version_of(connection, ctx)

        done: list[PlannedOp] = []
        folders: set[str] = set()

        def run(op: PlanOp) -> None:
            planned = decode_op(op.payload or {})
            folder = self._execute(ctx, planned, plan_id, job_id)
            if folder is REFUSED:
                return  # a last look refused it: not written, not reported as done
            done.append(planned)
            if isinstance(folder, str):
                folders.add(folder)

        store = PlanStore(
            self._engine, clock=self._clock, current_version=current, step_version=inputs
        )
        try:
            store.apply(plan_id, if_match=if_match, run=run)
        finally:
            if self._request_scan is not None:
                for folder in sorted(folders):  # one targeted scan per series folder
                    self._request_scan(ctx.remote_folder(folder))
        log.info(
            "export.applied",
            plan_id=plan_id,
            ops_done=len(done),
            folders=len(folders),
            duration_ms=elapsed_ms(started),
        )
        return ApplyReport(plan_id, tuple(done))

    def require_names_checked(self, plan_id: str) -> None:
        """A stored plan is applied only if the names `plugin/parse` answers now are the ones it
        was made with: the same plan, computed again with the answers bound here, gives the same
        operations. A preview made without the answers of Kavita never passes (EF-EXP-045)."""
        self._require_verified()
        with self._engine.connect() as connection:
            plan = connection.execute(
                sa.select(coherence.export_plan).where(coherence.export_plan.c.plan_id == plan_id)
            ).one()
            stored = connection.execute(
                sa.select(coherence.export_op.c.payload)
                .where(coherence.export_op.c.plan_id == plan_id)
                .order_by(coherence.export_op.c.batch, coherence.export_op.c.position)
            ).scalars()
            wanted = [
                (p["kind"], p["unit_id"], p["export_item_id"], p["rel_path"]) for p in stored if p
            ]
        _ctx, _loaded, fresh = self._compute(str(plan.shelf_destination_id))
        if wanted != [(op.kind, op.unit_id, op.export_item_id, op.rel_path) for op in fresh.ops]:
            self._mark_stale(plan_id)
            raise StalePlanError("the names changed under plugin/parse: recompute the preview")

    def _mark_stale(self, plan_id: str) -> None:
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.update(coherence.export_plan)
                .where(coherence.export_plan.c.plan_id == plan_id)
                .where(coherence.export_plan.c.status == "planned")
                .values(status="stale", updated_at=self._clock.now())
            )

    def _execute(
        self,
        ctx: DestinationContext,
        planned: PlannedOp,
        plan_id: str,
        job_id: str,
    ) -> str | _Refused | None:
        with bound_contextvars(
            unit_id=planned.unit_id,
            series_id=planned.work_id,
            edition_id=planned.series.edition_id,
            target_id=ctx.target_id,
        ):
            started = time.perf_counter()
            folder = self._execute_bound(ctx, planned, plan_id, job_id)
            log.debug(
                "export.op_done",
                kind=planned.kind,
                duration_ms=elapsed_ms(started),
                wrote=isinstance(folder, str),
            )
            return folder

    def _execute_bound(
        self,
        ctx: DestinationContext,
        planned: PlannedOp,
        plan_id: str,
        job_id: str,
    ) -> str | _Refused | None:
        folder = planned.rel_path.split("/", 1)[0] if planned.rel_path else ""
        with self._engine.connect() as connection:
            if planned.series.export_series_id in paused_series(connection, ctx.destination_id):
                log.debug("export.series_paused", export_series_id=planned.series.export_series_id)
                return REFUSED  # paused since the plan was stored: nothing is written into it
        library = (
            library_resource(ctx.remote_library_id, planned.series.normalized_name)
            if ctx.remote_library_id is not None
            else None
        )
        with self._locks.hold(
            job_id, work=planned.work_id, series=f"{ctx.destination_id}/{folder}",
            library=library, timeout=LOCK_WAIT_SECONDS,
        ) as held:  # fmt: skip
            try:
                if self._superseded(ctx, planned):  # planned before this lock: look again
                    log.warning(
                        "export.op_superseded", kind=planned.kind, unit_id=planned.unit_id,
                        rel_path=planned.rel_path,
                    )  # fmt: skip
                    return REFUSED
                if planned.kind == "register":
                    self._register(ctx, planned)
                    return None
                if not self._write(ctx, planned, plan_id, held.ensure):
                    return REFUSED  # a volume the last look left as it was: nothing written
            except SeriesAgeGateClosedError:
                log.warning("export.series_age_unverified", edition_id=planned.series.edition_id)
                return REFUSED
            except FamilyBarrierClosedError:
                # read again under the `work` lock, in the very transaction that reserves the line
                # (a withdrawal takes the same lock): the shelf below 18+ no longer takes it
                log.warning("export.family_barrier", edition_id=planned.series.edition_id)
                return REFUSED
            except DestinationPermissionError:
                if self._preflight is None:
                    raise
                # waits for `target recheck`; the rest of the plan goes on, nothing is retried
                series_id = self._series_of(ctx, planned)
                if series_id is None:
                    raise
                self._preflight.mark_denied(ctx.destination_id, series_id)
                log.warning("export.write_denied", export_series_id=series_id)
                return REFUSED
        return ctx.local(folder)

    def _superseded(self, ctx: DestinationContext, planned: PlannedOp) -> bool:
        """Under the resource lock, the plan is checked again against the manifest as it is now:
        another export of the destination may have applied the very same plan between this plan
        and this lock. What the plan meant as a first write (a new item, or a line left `planned`
        or `error`) is not written again when the manifest already holds it. A volume decides
        again from the database in `_write_volume`."""
        if planned.unit_id is None:
            return False
        record, coverage = export.export_record, export.unit_coverage
        with self._engine.connect() as connection:
            if planned.export_item_id is not None:  # a line left `planned` or `error`
                state = connection.execute(
                    sa.select(record.c.state).where(
                        record.c.shelf_destination_id == ctx.destination_id,
                        record.c.export_item_id == planned.export_item_id,
                    )
                ).scalar()
                return state is not None and state not in ("planned", "error", *GONE_STATES)
            covers = record.c.export_item_id.in_(
                sa.select(coverage.c.carrier_id).where(
                    coverage.c.carrier_type == "export_item",
                    coverage.c.unit_id == planned.unit_id,
                )
            )
            same_path = (
                [record.c.rel_path == planned.rel_path] if planned.rel_path is not None else []
            )
            clash = sa.select(record.c.export_item_id).where(
                record.c.shelf_destination_id == ctx.destination_id,
                record.c.state.not_in(GONE_STATES),  # a freed path is free, as the planner says
                sa.or_(covers, *same_path),
            )
            return connection.execute(clash.limit(1)).first() is not None

    def _series_of(self, ctx: DestinationContext, planned: PlannedOp) -> str | None:
        """The export series of an operation, created by its reservation when it was new."""
        if planned.series.export_series_id is not None:
            return planned.series.export_series_id
        link = export.edition_export_series
        with self._engine.connect() as connection:
            found = connection.execute(
                sa.select(link.c.export_series_id).where(
                    link.c.edition_id == planned.series.edition_id,
                    link.c.remote_library_id == ctx.remote_library_id,
                )
            ).scalar()
        return str(found) if found is not None else None

    def _source(self, unit_id: str) -> ExportSource:
        return self._source_of(unit_id)[0]

    def _source_of(self, unit_id: str) -> tuple[ExportSource, bool]:
        """The file to read for the Unit, and whether it is an EPUB (the file `servable_epub`
        names, never another)."""
        served: ServableEpub | None = None
        with self._engine.connect() as connection:
            try:  # EF-BIB-051: an untrusted EPUB leaves by its derivative or not at all
                require_servable_derivative(connection, unit_id)
                if has_epub(connection, unit_id):
                    served = servable_epub(
                        self._engine,
                        self._fsops,
                        unit_id,
                        staging=self._staging if self._staging.is_dir() else None,
                    )
            except OriginalNotServableError as error:
                raise SourceMissingError(f"{unit_id}: {error.reason}") from None
            source = export_sources(connection, [unit_id], formats=("cbz", "epub")).get(unit_id)
        if source is None or source.local_path is None:
            raise SourceMissingError(unit_id)
        if served is not None:  # the file that was verified is the one that leaves
            source = replace(source, local_path=served.path)
        return source, served is not None

    def _write(
        self, ctx: DestinationContext, planned: PlannedOp, plan_id: str, guard: Callable[[], None]
    ) -> bool:
        """Write one operation; False when a last look left the destination as it was (only a
        volume does: the file kept is refused, or the policy keeps what is served)."""
        if planned.rel_path is None:
            raise ValueError("a write operation has a path")
        if planned.unit_id is None:
            return self._write_volume(ctx, planned, plan_id, guard)
        source, is_epub = self._source_of(planned.unit_id)
        series_name = str(planned.frozen["series"])
        if is_epub:
            self._write_epub(ctx, planned, plan_id, source, guard)
            return True
        with self._engine.connect() as connection:
            xml = comicinfo_xml(connection, planned.unit_id, series_name)
        rendered = self._renderer.render(str(source.local_path), xml)
        try:
            if rendered.content_hash != planned.content_hash:
                raise SourceMissingError(f"{planned.unit_id}: the archive changed since the plan")
            self._place(
                ctx, planned, plan_id, source, rendered, guard, self._reserved(planned, rendered)
            )
        finally:
            rendered.file.close()
        return True

    def _write_volume(
        self, ctx: DestinationContext, planned: PlannedOp, plan_id: str, guard: Callable[[], None]
    ) -> bool:
        """A volume (an EPUB of a range) through `RangedExporter`, which decides again, from the
        database and the file kept, what the plan only forecast: the policy, the coverage of the
        table of contents, the tail already served. `place` is the pipeline's own placement;
        `replace` is the same-path upgrade (L16), which keeps the old file in the bin. Both come
        after the last look at the age gate and at the barrier of a family shelf, the replacement
        included: a volume whose age rose is not rewritten into the library of the children."""
        item_id = planned.export_item_id
        if item_id is None:
            raise ValueError("a volume operation names its item")
        self._require_age_gate(ctx, planned)
        with self._engine.connect() as connection:
            self._require_barrier(connection, ctx, planned)
        wrote: list[str] = []
        served = self._ranged.served_copy(ctx.destination_id, item_id)
        if served is None and self._has_line(ctx.destination_id, item_id):
            # a line without the fingerprint it was made from (restored, or from before): it is
            # not a first write and the length served is unknown, so nothing is rewritten
            log.warning("export.volume_served_unknown")
            return False

        def place(prepared: PreparedRange) -> None:
            rendered = self._render_volume(prepared, planned)
            try:
                self._place(
                    ctx, planned, plan_id, None, rendered, guard,
                    self._reserved_volume(planned, prepared, rendered),
                )  # fmt: skip
            finally:
                rendered.file.close()
            self._ranged.note_placed(ctx.destination_id, item_id, prepared)
            self._keep_volume_opf(item_id, rendered.original_opf)
            wrote.append("placed")

        def replace(prepared: PreparedRange) -> None:
            if served is None or served.spine_count is None:  # the guard of L17 has nothing to read
                log.warning("export.volume_served_unknown")
                return
            rendered = self._render_volume(prepared, planned)
            try:
                result = self._replace_volume(ctx, planned, prepared, rendered, guard)
            finally:
                rendered.file.close()
            if result is UpgradeOutcome.UPGRADED:
                wrote.append("replaced")
            else:  # a last look of L16 (a reader, a frozen root, a rewritten file): left alone
                log.info("export.volume_kept", reason=result.value)

        try:
            outcome = self._ranged.export(
                item_id, served=served, place=place, replace=replace,
            )  # fmt: skip
        except RangedExportError as error:  # nothing was written: the rest of the plan goes on
            log.warning("export.volume_refused", code=error.code)
            return False
        if outcome.warning is not None:
            log.info("export.volume_tail", warning=outcome.warning)
        return bool(wrote)

    def _has_line(self, destination_id: str, item_id: str) -> bool:
        table = export.export_record
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(table.c.state).where(
                        table.c.shelf_destination_id == destination_id,
                        table.c.export_item_id == item_id,
                        table.c.state.in_(("written", "scanned", "resolved", "adopted")),
                    )
                ).first()
                is not None
            )

    def _render_volume(self, prepared: PreparedRange, planned: PlannedOp) -> RenderedEpub:
        """The derivative of the file kept, its OPF edited (series, volume number, rights)."""
        if prepared.source_hash != planned.content_hash:
            raise SourceMissingError(f"{planned.export_item_id}: the volume changed since the plan")
        with self._engine.connect() as connection:
            edit = volume_opf_edit(
                connection, planned.series.edition_id, str(planned.frozen["series"]),
                str(planned.frozen["number_norm"]),
            )  # fmt: skip
        return self._ranged_renderer.render(str(prepared.path), edit)

    def _replace_volume(
        self, ctx: DestinationContext, planned: PlannedOp, prepared: PreparedRange,
        rendered: RenderedEpub, guard: Callable[[], None],
    ) -> UpgradeOutcome:  # fmt: skip
        target = ctx.local(str(planned.rel_path))
        opened = (
            self._sessions.open(target, guard=guard) if self._sessions else nullcontext(self._fsops)
        )
        with opened as writer:
            upgrade = SamePathUpgrade(
                self._engine, writer, locate=self._locate, data_dir=self._data_dir,
                clock=self._clock, bus=self._operating_bus, mirror=self.mirror,
            )  # fmt: skip
            result = upgrade.replace_volume(
                ctx.destination_id, str(planned.export_item_id), data=rendered.file,
                file_hash=rendered.file_hash,
                content_hash=prepared.payload_hash or prepared.source_hash,
                source_hash=prepared.source_hash, spine_count=prepared.spine_count,
            )  # fmt: skip
        return result.outcome

    def _keep_volume_opf(self, item_id: str, original_opf: bytes) -> None:
        """The original OPF, kept under the first Unit the volume covers (so that the purge of
        that Unit takes it too); best effort, as for a chapter."""
        with self._engine.connect() as connection:
            unit_id = connection.execute(
                sa.select(export.unit_coverage.c.unit_id)
                .where(
                    export.unit_coverage.c.carrier_type == "export_item",
                    export.unit_coverage.c.carrier_id == item_id,
                )
                .order_by(export.unit_coverage.c.position)
                .limit(1)
            ).scalar()
        if unit_id is None:
            return
        try:
            keep_origin_opf(self._data_dir, str(unit_id), original_opf)
        except (OSError, ValueError):
            log.warning("export.origin_opf_not_kept", item_id=item_id)

    def _write_epub(
        self, ctx: DestinationContext, planned: PlannedOp, plan_id: str, source: ExportSource,
        guard: Callable[[], None],
    ) -> None:  # fmt: skip
        """The passthrough of L08: the OPF edited (no age, D1), every other entry byte for byte;
        the original OPF kept in /data/meta once the file is placed (EF-BIB-048)."""
        unit_id = str(planned.unit_id)
        if source.content_hash != planned.content_hash:
            raise SourceMissingError(f"{unit_id}: the book changed since the plan")
        self._require_age_gate(ctx, planned)
        with self._engine.connect() as connection:
            edit = epub_opf_edit(connection, unit_id, str(planned.frozen["series"]))
        rendered = self._epub_renderer.render(str(source.local_path), edit)
        try:
            self._place(
                ctx, planned, plan_id, source, rendered, guard,
                self._reserved_epub(planned, source, rendered),
            )  # fmt: skip
        finally:
            rendered.file.close()
        try:  # best effort: a full disk costs the metadata, never the book
            keep_origin_opf(self._data_dir, unit_id, rendered.original_opf)
        except (OSError, ValueError):
            log.warning("export.origin_opf_not_kept", unit_id=unit_id)

    def _require_age_gate(self, ctx: DestinationContext, planned: PlannedOp) -> None:
        """The last look before an EPUB is written, under the locks of the operation (the
        library one among them), from the database and from Kavita now, not from the plan: a
        series with an age to push takes a file only once its age reads back locked, on every
        Kavita series its files are in, at the value wanted now; or as its very first file while
        Kavita answers and no account that sees the "unknown" series is restricted below its age
        (EF-EXP-040). Pull destinations have no Kavita series."""
        if ctx.transport == "pull":
            return
        gate = self._age_gate
        with self._engine.connect() as connection:
            age = edition_age(connection, planned.series.edition_id)
            if age is None:
                return  # nobody rated it: no age to push (a shelf below 18+ refused it already)
            series_id = self._series_of(ctx, planned)
            if series_id is not None and series_id in gate.verified:
                if self._reads_locked(connection, ctx, series_id):
                    return
                raise SeriesAgeGateClosedError(planned.series.edition_id)
            first = series_id is None or series_id not in seeded_series(
                connection, ctx.destination_id
            )
        if not (gate.open and first and self._hidden_while_unknown(age, gate)):
            raise SeriesAgeGateClosedError(planned.series.edition_id)

    def _hidden_while_unknown(self, age: AgeRating, gate: AgeGateFacts) -> bool:
        """No account that sees the "unknown" series is restricted below this age: by what the
        gate read, and by the accounts read again now (one may have been added or changed)."""
        if gate.unknowns_tier is not None and age_rank(age) > gate.unknowns_tier:
            return False
        if self._tier_reader is None:
            return False
        try:
            now = self._tier_reader()
        except Exception as error:  # Kavita does not answer now: fail closed
            log.info("export.accounts_reread_failed", reason=type(error).__name__)
            return False
        return now is None or age_rank(age) <= now

    def _reads_locked(
        self, connection: sa.Connection, ctx: DestinationContext, series_id: str
    ) -> bool:
        (task,) = due_ages(connection, ctx.destination_id, only=series_id) or (None,)
        if task is None or not task.kavita_series_ids or self._age_reader is None:
            return False
        owned = FieldRegistry(connection).get(ctx.destination_id, series_id, AGE_RATING)
        shown: list[tuple[int, bool]] = []
        for kavita_id in task.kavita_series_ids:
            try:
                reading = self._age_reader(kavita_id)
            except Exception as error:  # Kavita does not answer now: fail closed
                log.info(
                    "export.age_reread_failed", export_series_id=series_id,
                    reason=type(error).__name__,
                )  # fmt: skip
                return False
            if reading is not None:  # a series Kavita no longer shows is no half of this one
                shown.append(reading)
        if len(shown) != 1:  # none indexed, or Kavita split the series
            return False
        ((rating, locked),) = shown
        return verified_age(task.wanted, owned, locked=locked, kavita_value=rating)

    def _place(
        self,
        ctx: DestinationContext,
        planned: PlannedOp,
        plan_id: str,
        source: ExportSource | None,
        rendered: RenderedCbz | RenderedEpub,
        guard: Callable[[], None],
        reserved: dict[str, Any],
    ) -> None:
        """02 section 5: the series and the manifest line are reserved and the intent committed
        (`pending`), then the file is placed atomically, then the manifest line is completed and
        the intent closed in one transaction. A crash in between is settled at boot by
        `resolve_intents`: the expected file is at the target, the line is completed; nothing is
        there, the intent is closed; anything else is a drift and nothing is touched."""
        target = ctx.local(str(planned.rel_path))
        # reservation: a clash stops here, nothing placed
        with write_transaction(self._engine) as connection:
            self._require_barrier(connection, ctx, planned)
            item_id = self._item(connection, ctx, planned, source)
            self._record(connection, ctx, planned, item_id, reserved)
        journal = IntentJournal(self._engine, self._fsops, clock=self._clock)
        intent = journal.record(
            op="write", dst=target, expected_sha256=rendered.file_hash, plan_id=plan_id
        )
        journal.mark_applying(intent)
        opened = (
            self._sessions.open(target, guard=guard) if self._sessions else nullcontext(self._fsops)
        )
        try:
            with opened as writer:
                try:
                    identity = writer.write_atomic(
                        target, rendered.file, parents=True, replace=False,
                        verify_digest=lambda: rendered.file_hash.removeprefix("sha256:"),
                    )  # fmt: skip
                except PermissionError as error:
                    raise DestinationPermissionError(*error.args) from error
                with write_transaction(self._engine) as connection:
                    complete_write(
                        connection, self._fsops, self._locate, self._clock.now(),
                        destination_id=ctx.destination_id, item_id=item_id, target=target,
                        identity=identity,
                    )  # fmt: skip
                    journal.mark_done(intent, within=connection)
                # same write session, after the file and its manifest line (EF-EXP-063, I33)
                try:
                    self.mirror.sync_destination(
                        ctx.destination_id, folder=str(planned.rel_path).split("/", 1)[0],
                        writer=writer,
                    )  # fmt: skip
                except Exception:  # the file is placed and committed: the next run heals it
                    log.warning("mirror_write_failed", destination=ctx.destination_id)
        except BaseException:
            if self._fsops.observe(target) is None:  # nothing was placed: nothing to settle
                journal.cancel(intent)
            raise

    @staticmethod
    def _require_barrier(
        connection: sa.Connection, ctx: DestinationContext, planned: PlannedOp
    ) -> None:
        if not barrier_holds(connection, ctx.destination_id, planned.series.edition_id):
            raise FamilyBarrierClosedError(planned.series.edition_id)

    @staticmethod
    def _reserved(planned: PlannedOp, rendered: RenderedCbz) -> dict[str, Any]:
        return {
            "locator_type": "path", "rel_path": planned.rel_path, "format": "cbz",
            "template_ref": str(planned.frozen["template_ref"]),
            "content_hash": rendered.content_hash, "sidecar_sha256": rendered.sidecar_sha256,
            "file_hash": rendered.file_hash, "state": "planned",
        }  # fmt: skip

    @staticmethod
    def _reserved_epub(
        planned: PlannedOp, source: ExportSource, rendered: RenderedEpub
    ) -> dict[str, Any]:
        return {
            "locator_type": "path", "rel_path": planned.rel_path, "format": "epub",
            "template_ref": str(planned.frozen["template_ref"]),
            "content_hash": source.content_hash, "sidecar_sha256": None,
            "file_hash": rendered.file_hash, "state": "planned",
        }  # fmt: skip

    @staticmethod
    def _reserved_volume(
        planned: PlannedOp, prepared: PreparedRange, rendered: RenderedEpub
    ) -> dict[str, Any]:
        return {
            "locator_type": "path", "rel_path": planned.rel_path, "format": "epub",
            "template_ref": str(planned.frozen["template_ref"]),
            # the payload (anti-echo, as for a chapter); the kept file it came from rides in the
            # sidecar column, which an EPUB has no use for
            "content_hash": prepared.payload_hash or prepared.source_hash,
            "sidecar_sha256": prepared.source_hash,
            "file_hash": rendered.file_hash, "spine_count": prepared.spine_count,
            "state": "planned",
        }  # fmt: skip

    def _register(self, ctx: DestinationContext, planned: PlannedOp) -> None:
        """A `pull` destination: the manifest line only, no locator, nothing on disk."""
        source = self._source(planned.unit_id) if planned.unit_id is not None else None
        now = self._clock.now()
        with write_transaction(self._engine) as connection:
            self._require_barrier(connection, ctx, planned)
            item_id = self._item(connection, ctx, planned, source)
            values = {
                "locator_type": "none",
                "content_hash": source.content_hash if source is not None else planned.content_hash,
                "template_ref": str(planned.frozen["template_ref"]), "written_by_shelfhold_at": now,
                "exported_at": now, "state": "written",
            }  # fmt: skip
            self._record(connection, ctx, planned, item_id, values)

    def _item(
        self, connection: sa.Connection, ctx: DestinationContext, planned: PlannedOp,
        source: ExportSource | None,
    ) -> str:  # fmt: skip
        if ctx.remote_library_id is not None:
            ensure_export_series(
                connection, shelf_destination_id=ctx.destination_id,
                remote_library_id=ctx.remote_library_id, ref=planned.series, now=self._clock.now(),
            )  # fmt: skip
        if planned.unit_id is None:  # a volume: the item the keeper made, shared by destinations
            return str(planned.export_item_id)
        if planned.export_item_id is not None:
            return planned.export_item_id
        if source is None:
            raise ValueError("a new item of a Unit has a source")
        return create_export_item(
            connection, edition_id=planned.series.edition_id, unit_id=planned.unit_id,
            kind="unit", frozen_attrs=dict(planned.frozen), content_id=source.content_id,
        )  # fmt: skip

    @staticmethod
    def _record(
        connection: sa.Connection, ctx: DestinationContext, planned: PlannedOp, item_id: str,
        values: dict[str, Any],
    ) -> None:  # fmt: skip
        table = export.export_record
        if planned.export_item_id is not None:  # a record left `planned` or `error`, or a volume
            updated = connection.execute(
                sa.update(table)
                .where(
                    table.c.shelf_destination_id == ctx.destination_id,
                    table.c.export_item_id == item_id,
                )
                .values(**values)
            ).rowcount
            if updated == 0:  # the item of a volume exists before its first line does
                connection.execute(
                    sa.insert(table).values(
                        shelf_destination_id=ctx.destination_id, export_item_id=item_id, **values
                    )
                )
        else:
            connection.execute(
                sa.insert(table).values(
                    shelf_destination_id=ctx.destination_id, export_item_id=item_id, **values
                )
            )
