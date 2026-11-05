"""The export pipeline: plan, store, apply (EF-EXP-007, EF-EXP-008, EF-EXP-058).

`preview` (the dry run) and `run` (plan then apply) go through the same `_compute`, hence the same
`plan_destination`. A preview is stored as a versioned plan (`PlanStore`, 412 when stale); applying
it replays the stored operations, each under the resource locks of its Work and series folder, the
manifest being committed right after the atomic write. The archive is rendered (pages as received,
ComicInfo last) into the tree of the destination; a `pull` destination only gets a manifest line,
nothing is written.
"""

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application.export_items import create_export_item, ensure_export_series
from shelfhold.export.application.intents import open_intent_paths
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.plans import PlanOp, PlanStore
from shelfhold.export.application.rendering import CbzRenderer, RenderedCbz, comicinfo_data
from shelfhold.export.application.snapshot import (
    DestinationContext,
    LoadedRequest,
    load_context,
    load_request,
    version_of,
)
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
from shelfhold.infrastructure.packaging.comicinfo import render_comicinfo
from shelfhold.infrastructure.schema import coherence, export
from shelfhold.library.public import (
    ExportSource,
    RootLocator,
    export_sources,
    record_destination_copy,
)

__all__ = [
    "ApplyReport",
    "ExportPipeline",
    "PlanPreview",
    "SourceMissingError",
    "UnverifiedParseError",
    "decode_op",
    "encode_op",
]

PLAN_KIND = "export"
LOCK_WAIT_SECONDS = 30.0


class SourceMissingError(Exception):
    """The received archive of a planned Unit is gone: the plan is out of date."""

    code = "export.source_missing"


class UnverifiedParseError(Exception):
    """No `plugin/parse` answers behind this pipeline: it can plan, never write (EF-EXP-045)."""

    code = "target.route_missing"


class WriteSessionOpener(Protocol):
    def open(self, *paths: str, guard: Callable[[], None] | None = None) -> Any: ...


@dataclass(frozen=True)
class PlanPreview:
    plan_id: str
    base_version: int
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
        segment_policy_confirmed: bool = False,
        parse_verified: bool = True,
        renderer: CbzRenderer | None = None,
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

    def _require_verified(self) -> None:
        if not self._parse_verified:
            raise UnverifiedParseError("names are not checked by plugin/parse: preview only")

    # -- planning --------------------------------------------------------------------------------

    def _compute(self, destination_id: str) -> tuple[DestinationContext, LoadedRequest, Plan]:
        """The one way to a plan: the dry run and the application both come through here."""
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            covered = open_intent_paths(connection)
            loaded = load_request(
                connection, ctx,
                exists=lambda path: self._fsops.observe(path) is not None,
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
        """Plan, then apply; a plan with nothing to do is not even stored (idempotence)."""
        self._require_verified()
        ctx, loaded, plan = self._compute(destination_id)
        if not plan.ops:
            return ApplyReport(None, ())
        plan_id = self._store(ctx, loaded, plan)
        return self.apply(plan_id, if_match=loaded.version, job_id=job_id)

    # -- applying --------------------------------------------------------------------------------

    def apply(self, plan_id: str, *, if_match: int, job_id: str) -> ApplyReport:
        """Apply a stored plan once. `StalePlanError` (412) when anything moved since."""
        self._require_verified()
        with self._engine.connect() as connection:
            destination_id = connection.execute(
                sa.select(coherence.export_plan.c.shelf_destination_id).where(
                    coherence.export_plan.c.plan_id == plan_id
                )
            ).scalar_one()
            ctx = load_context(connection, destination_id)

        def current() -> int:
            with self._engine.connect() as connection:
                return version_of(connection, ctx)

        done: list[PlannedOp] = []
        folders: set[str] = set()

        def run(op: PlanOp) -> None:
            planned = decode_op(op.payload or {})
            folder = self._execute(ctx, planned, plan_id, job_id)
            done.append(planned)
            if folder is not None:
                folders.add(folder)

        store = PlanStore(self._engine, clock=self._clock, current_version=current)
        try:
            store.apply(plan_id, if_match=if_match, run=run)
        finally:
            if self._request_scan is not None:
                for folder in sorted(folders):  # one targeted scan per series folder
                    self._request_scan(ctx.remote_folder(folder))
        return ApplyReport(plan_id, tuple(done))

    def _execute(
        self, ctx: DestinationContext, planned: PlannedOp, plan_id: str, job_id: str
    ) -> str | None:
        folder = planned.rel_path.split("/", 1)[0] if planned.rel_path else ""
        with self._locks.hold(
            job_id, work=planned.work_id, series=f"{ctx.destination_id}/{folder}",
            timeout=LOCK_WAIT_SECONDS,
        ) as held:  # fmt: skip
            if planned.kind == "register":
                self._register(ctx, planned)
                return None
            self._write(ctx, planned, plan_id, held.ensure)
        return ctx.local(folder)

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
            xml = render_comicinfo(comicinfo_data(connection, planned.unit_id, series_name)).xml
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
        target = ctx.local(str(planned.rel_path))
        # One atomic step (stage, check, rename): there is no half-written file for the journal to
        # settle. A crash between the rename and the commit below leaves a file the manifest does
        # not know; the next plan finds the path occupied and blocks (`export.destination_occupied`)
        # instead of overwriting it, until the reconciliation of M1b adopts or removes it.
        opened = (
            self._sessions.open(target, guard=guard) if self._sessions else nullcontext(self._fsops)
        )
        with opened as writer:
            identity = writer.write_atomic(
                target, rendered.file, parents=True, replace=False,
                verify_digest=lambda: rendered.file_hash.removeprefix("sha256:"),
            )  # fmt: skip
        root_id, root_rel = self._locate(target)
        now = self._clock.now()
        with self._engine.begin() as connection:
            item_id = self._item(connection, ctx, planned, source)
            values = {
                "locator_type": "path", "rel_path": planned.rel_path, "format": "cbz",
                "template_ref": str(planned.frozen["template_ref"]), "size": identity.size,
                "mtime_ns": identity.mtime_ns, "content_hash": rendered.content_hash,
                "sidecar_sha256": rendered.sidecar_sha256, "file_hash": rendered.file_hash,
                "written_by_shelfhold_at": now, "exported_at": now, "state": "written",
            }  # fmt: skip
            self._record(connection, ctx, planned, item_id, values)
            record_destination_copy(
                connection, unit_id=planned.unit_id, export_item_id=item_id,
                content_hash=rendered.content_hash, file_hash=rendered.file_hash,
                physical_root_id=root_id, rel_path=root_rel, size=identity.size,
                mtime_ns=identity.mtime_ns, ino=identity.ino,
                dev=self._fsops.device_of(target), verified_at=now,
            )  # fmt: skip

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
