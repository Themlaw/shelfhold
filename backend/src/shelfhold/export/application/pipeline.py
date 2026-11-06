"""The export pipeline: plan, store, apply (EF-EXP-007, EF-EXP-008, EF-EXP-058).

`preview` (the dry run) and `run` (plan then apply) go through the same `_compute`, hence the same
`plan_destination`. A preview is stored as a versioned plan (`PlanStore`, 412 when stale); applying
it replays the stored operations, each under the resource locks of its Work and series folder, the
manifest being committed right after the atomic write. The archive is rendered (pages as received,
ComicInfo last) into the tree of the destination; a `pull` destination only gets a manifest line,
nothing is written.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.events.bus import EventBus
from shelfhold.export.application import blocked_memo
from shelfhold.export.application.export_items import create_export_item, ensure_export_series
from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.locks import ResourceLocks, library_resource
from shelfhold.export.application.manifest_write import complete_write
from shelfhold.export.application.mirror import ProvenanceMirror
from shelfhold.export.application.plans import PlanOp, PlanStore, StalePlanError
from shelfhold.export.application.rendering import CbzRenderer, RenderedCbz, comicinfo_xml
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.export.application.snapshot import (
    DestinationContext,
    LoadedRequest,
    inputs_version_of,
    load_context,
    load_request,
    paused_series,
    version_of,
)
from shelfhold.export.application.target_state import ensure_active
from shelfhold.export.domain.naming import Parsed
from shelfhold.export.domain.planner import (
    Blocked,
    Plan,
    PlannedOp,
    PlanWarning,
    SeriesRef,
    plan_destination,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import coherence, export
from shelfhold.library.public import (
    ExportSource,
    RootLocator,
    export_sources,
)

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


def wanted_names(
    engine: Engine, destination_id: str, *, segment_policy_confirmed: bool = False
) -> tuple[list[str], str | None]:
    """The names a plan of this destination asks `plugin/parse` about, and the Kavita library
    type to ask with. Read from the database alone: no disk is looked at, so an empty answer
    (nothing to write) costs the disks nothing (ENF-OPS-014)."""
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
        )  # fmt: skip
        kind = connection.execute(
            sa.select(export.shelf_destination.c.kavita_library_type).where(
                export.shelf_destination.c.id == destination_id
            )
        ).scalar()
    if blocked_memo.known(engine, destination_id, loaded.version):
        return [], kind  # the disk already said "taken" for these inputs: nothing to ask or write
    plan_destination(loaded.request, record)
    return sorted(set(asked)), kind


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
        self._renderer = renderer or CbzRenderer(fsops, staging_dir=data_dir / "tmp")
        self._locks = ResourceLocks(engine, clock=clock)
        self.mirror = mirror or ProvenanceMirror(
            engine, fsops, clock=clock, operating_bus=operating_bus
        )
        self._detector = detector  # EF-EXP-043: looked at before every export
        self._gate = gate  # raises while the instance is `restored_behind` (EF-INT-026)
        self._preflight: Preflight | None = None
        self._switchover: SwitchoverCheck | None = None

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

    def wanted_names(self, destination_id: str) -> tuple[list[str], str | None]:
        """See the module function: fetched in bulk, then `bind_parse` makes the plan applicable."""
        return wanted_names(
            self._engine, destination_id, segment_policy_confirmed=self._segment_policy_confirmed
        )

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
            )  # fmt: skip
        return ctx, loaded, plan_destination(loaded.request, self._parse)

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
            return ApplyReport(None, ())
        if blocked_memo.known(self._engine, destination_id, wanted.version):
            return ApplyReport(None, ())  # the disk said "taken" for these inputs: ask no more
        if self._detector is not None:
            self._detector(destination_id)
        if self._switchover is not None:
            self._switchover.require_clear(destination_id)
        if self._preflight is not None:
            with self._root_session(destination_id):  # the probe writes: under the lease too
                self._preflight.require_root(destination_id)
        ctx, loaded, plan = self._compute(destination_id)
        self._heal_mirror(ctx)
        if not plan.ops:
            # only what the disk alone decides: a name Kavita misreads depends on Kavita, which
            # can change its mind with no change of ours
            if plan.blocked and all(b.code == "export.destination_occupied" for b in plan.blocked):
                blocked_memo.remember(self._engine, destination_id, loaded.version)
            return ApplyReport(None, ())
        blocked_memo.forget(self._engine, destination_id)
        plan_id = self._store(ctx, loaded, plan)
        return self.apply(plan_id, if_match=loaded.version, job_id=job_id)

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

    def apply(self, plan_id: str, *, if_match: str, job_id: str) -> ApplyReport:
        """Apply a stored plan once. `StalePlanError` (412) when anything moved since."""
        self._require_writable()
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
            done.append(planned)
            if folder is not None:
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
            wanted = [(p["kind"], p["unit_id"], p["rel_path"]) for p in stored if p]
        _ctx, _loaded, fresh = self._compute(str(plan.shelf_destination_id))
        if wanted != [(op.kind, op.unit_id, op.rel_path) for op in fresh.ops]:
            self._mark_stale(plan_id)
            raise StalePlanError("the names changed under plugin/parse: recompute the preview")

    def _mark_stale(self, plan_id: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(coherence.export_plan)
                .where(coherence.export_plan.c.plan_id == plan_id)
                .where(coherence.export_plan.c.status == "planned")
                .values(status="stale", updated_at=self._clock.now())
            )

    def _execute(
        self, ctx: DestinationContext, planned: PlannedOp, plan_id: str, job_id: str
    ) -> str | None:
        folder = planned.rel_path.split("/", 1)[0] if planned.rel_path else ""
        with self._engine.connect() as connection:
            if planned.series.export_series_id in paused_series(connection, ctx.destination_id):
                return None  # paused since the plan was stored: nothing is written into it
        library = (
            library_resource(ctx.remote_library_id, planned.series.normalized_name)
            if ctx.remote_library_id is not None
            else None
        )
        with self._locks.hold(
            job_id, work=planned.work_id, series=f"{ctx.destination_id}/{folder}",
            library=library, timeout=LOCK_WAIT_SECONDS,
        ) as held:  # fmt: skip
            if planned.kind == "register":
                self._register(ctx, planned)
                return None
            try:
                self._write(ctx, planned, plan_id, held.ensure)
            except DestinationPermissionError:
                if self._preflight is None:
                    raise
                # waits for `target recheck`; the rest of the plan goes on, nothing is retried
                series_id = self._series_of(ctx, planned)
                if series_id is None:
                    raise
                self._preflight.mark_denied(ctx.destination_id, series_id)
                return None
        return ctx.local(folder)

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
        with self._engine.connect() as connection:
            source = export_sources(connection, [unit_id]).get(unit_id)
        if source is None or source.local_path is None:
            raise SourceMissingError(unit_id)
        return source

    def _write(
        self, ctx: DestinationContext, planned: PlannedOp, plan_id: str, guard: Callable[[], None]
    ) -> None:
        if planned.rel_path is None:
            raise ValueError("a write operation has a path")
        source = self._source(planned.unit_id)
        series_name = str(planned.frozen["series"])
        with self._engine.connect() as connection:
            xml = comicinfo_xml(connection, planned.unit_id, series_name)
        rendered = self._renderer.render(str(source.local_path), xml)
        try:
            if rendered.content_hash != planned.content_hash:
                raise SourceMissingError(f"{planned.unit_id}: the archive changed since the plan")
            self._place(ctx, planned, plan_id, source, rendered, guard)
        finally:
            rendered.file.close()

    def _place(
        self,
        ctx: DestinationContext,
        planned: PlannedOp,
        plan_id: str,
        source: ExportSource,
        rendered: RenderedCbz,
        guard: Callable[[], None],
    ) -> None:
        """02 section 5: the series and the manifest line are reserved and the intent committed
        (`pending`), then the file is placed atomically, then the manifest line is completed and
        the intent closed in one transaction. A crash in between is settled at boot by
        `resolve_intents`: the expected file is at the target, the line is completed; nothing is
        there, the intent is closed; anything else is a drift and nothing is touched."""
        target = ctx.local(str(planned.rel_path))
        with self._engine.begin() as connection:  # reservation: a clash stops here, nothing placed
            item_id = self._item(connection, ctx, planned, source)
            self._record(connection, ctx, planned, item_id, self._reserved(planned, rendered))
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
                with self._engine.begin() as connection:
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
    def _reserved(planned: PlannedOp, rendered: RenderedCbz) -> dict[str, Any]:
        return {
            "locator_type": "path", "rel_path": planned.rel_path, "format": "cbz",
            "template_ref": str(planned.frozen["template_ref"]),
            "content_hash": rendered.content_hash, "sidecar_sha256": rendered.sidecar_sha256,
            "file_hash": rendered.file_hash, "state": "planned",
        }  # fmt: skip

    def _register(self, ctx: DestinationContext, planned: PlannedOp) -> None:
        """A `pull` destination: the manifest line only, no locator, nothing on disk."""
        source = self._source(planned.unit_id)
        now = self._clock.now()
        with self._engine.begin() as connection:
            item_id = self._item(connection, ctx, planned, source)
            values = {
                "locator_type": "none", "content_hash": source.content_hash,
                "template_ref": str(planned.frozen["template_ref"]), "written_by_shelfhold_at": now,
                "exported_at": now, "state": "written",
            }  # fmt: skip
            self._record(connection, ctx, planned, item_id, values)

    def _item(
        self, connection: sa.Connection, ctx: DestinationContext, planned: PlannedOp,
        source: ExportSource,
    ) -> str:  # fmt: skip
        if ctx.remote_library_id is not None:
            ensure_export_series(
                connection, shelf_destination_id=ctx.destination_id,
                remote_library_id=ctx.remote_library_id, ref=planned.series, now=self._clock.now(),
            )  # fmt: skip
        if planned.export_item_id is not None:
            return planned.export_item_id
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
        if planned.export_item_id is not None:  # a record left `planned` or `error`
            connection.execute(
                sa.update(table)
                .where(
                    table.c.shelf_destination_id == ctx.destination_id,
                    table.c.export_item_id == item_id,
                )
                .values(**values)
            )
        else:
            connection.execute(
                sa.insert(table).values(
                    shelf_destination_id=ctx.destination_id, export_item_id=item_id, **values
                )
            )
