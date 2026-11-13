"""Rename plans: a template change, a reshelving or a binding migration is a plan of renames,
never a deletion followed by a recreation (EF-EXP-030, EF-EXP-037, EF-EXP-041, I22).

The files already exported keep their bytes and their inode: the plan only changes their names and
the folder of their series. A plan is made of batches, one per series folder; the application of a
batch leaves a consistent state (the manifest and the replicas follow each file in the very
transaction that closes its intent) and asks for one scan. A series folder whose files all go to
one new folder is moved by **one** rename (atomic), not file by file.

What is never done:

- a path outside the managed root, in its reserved area, or through a link (the destination's
  parents are looked at as the disk shows them, a link is never followed);
- a name that is taken (on the disk or by another item of the plan, with Kavita's case folding):
  nothing is replaced;
- a path that an open intent covers (I9);
- a plan applied without the confirmation when readers of Kavita would lose their progress
  (EF-EXP-037: before M4 the progress is not carried over, so the count is told and the
  confirmation asked; a count that could not be read is not "nobody").

The reader count belongs to the admin who previews and applies: it is in the answer of these two
doors and nowhere else (no event, no audit line, no log carries it).
"""

import posixpath
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.export.application.intents import (
    IntentJournal,
    MoveDirStep,
    MoveRefusedError,
    OpenIntent,
    RenameStep,
    open_intent_paths,
    within_any,
)
from shelfhold.export.application.locks import ResourceLocks, path_resource
from shelfhold.export.application.plans import PlanOp, PlanStore, StalePlanError
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.naming import FrozenAttrs, NamingTemplate, export_path
from shelfhold.export.domain.physical_roots import RECEIVED_DIR
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.schema import coherence, export
from shelfhold.library.public import relocate_destination_copy

log = structlog.get_logger(__name__)

PLAN_KIND = "rename_plan"
RESERVED_AREA = ".shelfhold"
_LIVE_STATES = ("written", "scanned", "resolved", "adopted")

__all__ = [
    "PLAN_KIND",
    "ItemMove",
    "PlannedRename",
    "ProgressLossWarning",
    "ReaderCount",
    "ReaderCounter",
    "RenameBook",
    "RenameConfirmationRequiredError",
    "RenamePreview",
    "RenameRefusedError",
    "RenameReport",
    "validate_rel_path",
]


class RenameRefusedError(Exception):
    """A plan that cannot be made or a step that cannot be run; `reason` is a closed word."""

    code = "rename.refused"
    status = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RenameConfirmationRequiredError(Exception):
    """The plan makes readers of Kavita lose their progress and was not confirmed (EF-EXP-037)."""

    code = "rename.confirmation_required"
    status = 409

    def __init__(self, readers: int, *, exact: bool = True) -> None:
        super().__init__(f"{readers} readers")
        self.readers = readers
        self.exact = exact


@dataclass(frozen=True)
class ReaderCount:
    """How many accounts of Kavita have read in the folders; `exact` is false when Kavita (or a
    member's own token) could not be asked: the count is then a floor, not the truth."""

    count: int
    exact: bool = True


# `folders`: the folders as Kavita sees them. Bound by the composition root; none bound means no
# reading server is behind the destination.
ReaderCounter = Callable[[Sequence[str]], ReaderCount]


@dataclass(frozen=True)
class ItemMove:
    export_item_id: str
    new_rel_path: str


@dataclass(frozen=True)
class PlannedRename:
    """One step of a preview, paths relative to the root of the destination. `series_dir` moves a
    whole folder by one rename; `file` renames one exported file."""

    kind: str
    src: str
    dst: str
    export_item_id: str | None = None


@dataclass(frozen=True)
class ProgressLossWarning:
    """ "The Kavita progress of N readers will be lost" (EF-EXP-037)."""

    readers: int
    exact: bool
    series: tuple[str, ...]
    code: str = "progress_loss"


@dataclass(frozen=True)
class RenamePreview:
    plan_id: str | None  # None: nothing to rename, nothing stored
    base_version: str
    steps: tuple[PlannedRename, ...]
    batches: int
    blocked: tuple[dict[str, Any], ...]
    warnings: tuple[ProgressLossWarning, ...]


@dataclass(frozen=True)
class RenameReport:
    plan_id: str
    steps_done: int
    settled: int = 0  # intents a stop left open that the resume settled first


@dataclass(frozen=True)
class _Record:
    item_id: str
    rel_path: str
    template_ref: str | None
    frozen: dict[str, Any]


def validate_rel_path(rel: str) -> str:
    """The relative path of a destination, or the reason it is refused. Closed answers:
    `outside_root` (absolute, `..`), `reserved` (`.shelfhold`), `invalid_path` (anything else
    that is not a plain relative path: empty parts, `.`, a backslash, a control character)."""
    if not rel or "\\" in rel or any(ord(c) < 32 or ord(c) == 127 for c in rel):
        raise RenameRefusedError("invalid_path")
    if rel.startswith("/"):
        raise RenameRefusedError("outside_root")
    parts = rel.split("/")
    if ".." in parts:
        raise RenameRefusedError("outside_root")
    if any(part in ("", ".") for part in parts):
        raise RenameRefusedError("invalid_path")
    if parts[0].casefold() == RESERVED_AREA or parts[0] == RECEIVED_DIR:
        raise RenameRefusedError("reserved")
    if len(parts) > 8 or any(len(part.encode()) > 255 for part in parts):
        raise RenameRefusedError("invalid_path")
    return rel


def _fold(text: str) -> str:
    return " ".join(text.casefold().split())


class RenameBook:
    """Plans and applies the renames of one library's destinations."""

    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        *,
        locate: Callable[[str], tuple[str, str]],
        clock: Clock,
        locks: ResourceLocks,
        request_scan: Callable[[str], None] | None = None,
        lock_wait_s: float = 30.0,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._locate = locate
        self._clock = clock
        self._locks = locks
        self._request_scan = request_scan
        self._lock_wait_s = lock_wait_s
        self._readers: ReaderCounter | None = None

    def bind_readers(self, counter: ReaderCounter | None) -> None:
        self._readers = counter

    # -- the preview ------------------------------------------------------------------------

    def preview(
        self,
        destination_id: str,
        *,
        template: NamingTemplate | None = None,
        moves: Sequence[ItemMove] = (),
    ) -> RenamePreview:
        """The plan, stored so that it is applied once; nothing is written on the disk."""
        with self._engine.connect() as conn:
            ctx = load_context(conn, destination_id)
            records = self._records(conn, destination_id)
            covered = open_intent_paths(conn)
            version = self._version(conn, ctx)
        if ctx.root_local is None:
            raise RenameRefusedError("no_local_root")
        blocked: list[dict[str, Any]] = []
        if template is not None:
            wanted = self._template_targets(records, template, blocked)
        else:
            wanted = self._move_targets(records, moves)
        wanted = {item: new for item, new in wanted.items() if new != records[item].rel_path}
        steps, batches = self._steps(ctx, records, wanted, covered)
        if not steps:
            return RenamePreview(None, version, (), 0, tuple(blocked), ())
        warnings = self._warnings(ctx, steps)
        files = [ctx.local(p) for s in steps for p in (s.src, s.dst)]
        files += [ctx.local(records[i].rel_path) for i in wanted]
        store = PlanStore(self._engine, clock=self._clock, current_version=lambda: version)
        by_batch: dict[int, list[PlanOp]] = defaultdict(list)
        for number, batch in enumerate(batches, start=1):
            for step in batch:
                by_batch[number].append(self._op_of(ctx, step, wanted, template, number))
        record = store.create(
            kind=PLAN_KIND,
            files=files,
            ops=[op for number in sorted(by_batch) for op in by_batch[number]],
            shelf_destination_id=destination_id,
        )
        log.info("rename.planned", plan_id=record.plan_id, steps=len(steps), batches=len(batches))
        return RenamePreview(
            record.plan_id,
            record.base_version,
            tuple(steps),
            len(batches),
            tuple(blocked),
            warnings,
        )

    def _op_of(
        self,
        ctx: DestinationContext,
        step: PlannedRename,
        wanted: dict[str, str],
        template: NamingTemplate | None,
        batch: int,
    ) -> PlanOp:
        payload: dict[str, Any] = {
            "scope": step.kind,
            "destination_id": ctx.destination_id,
            "old_rel": step.src,
            "new_rel": step.dst,
            "item_id": step.export_item_id,
            "template": template.dumps() if template is not None else None,
        }
        return PlanOp("rename", ctx.local(step.src), ctx.local(step.dst), payload, batch)

    # -- what moves -------------------------------------------------------------------------

    @staticmethod
    def _records(conn: Connection, destination_id: str) -> dict[str, _Record]:
        record, item = export.export_record, export.export_item
        rows = conn.execute(
            sa.select(
                record.c.export_item_id,
                record.c.rel_path,
                record.c.template_ref,
                item.c.frozen_attrs,
            )
            .join(item, item.c.id == record.c.export_item_id)
            .where(
                record.c.shelf_destination_id == destination_id,
                record.c.locator_type == "path",
                record.c.rel_path.isnot(None),
                record.c.state.in_(_LIVE_STATES),
            )
            .order_by(record.c.rel_path)
        ).all()
        return {
            r.export_item_id: _Record(
                r.export_item_id, r.rel_path, r.template_ref, dict(r.frozen_attrs or {})
            )
            for r in rows
        }

    @staticmethod
    def _version(conn: Connection, ctx: DestinationContext) -> str:
        from shelfhold.export.application.snapshot import version_of

        return version_of(conn, ctx)

    @staticmethod
    def _template_targets(
        records: dict[str, _Record], template: NamingTemplate, blocked: list[dict[str, Any]]
    ) -> dict[str, str]:
        wanted: dict[str, str] = {}
        for item_id, rec in records.items():
            if "number_norm" not in rec.frozen:  # a range volume, not named from its attributes
                continue
            try:
                frozen = replace(FrozenAttrs.stored(rec.frozen), template_ref=template.ref)
                path = export_path(frozen, template, PurePosixPath(rec.rel_path).suffix)
            except ValueError:
                blocked.append({"export_item_id": item_id, "reason": "name_invalid"})
                continue
            wanted[item_id] = path.rel_path
        return wanted

    @staticmethod
    def _move_targets(records: dict[str, _Record], moves: Sequence[ItemMove]) -> dict[str, str]:
        wanted: dict[str, str] = {}
        for move in moves:
            if move.export_item_id not in records:
                raise RenameRefusedError("unknown_item")
            wanted[move.export_item_id] = validate_rel_path(move.new_rel_path)
        return wanted

    def _steps(
        self,
        ctx: DestinationContext,
        records: dict[str, _Record],
        wanted: dict[str, str],
        covered: frozenset[str],
    ) -> tuple[list[PlannedRename], list[list[PlannedRename]]]:
        """The ordered steps and their batches (one per old top-level folder), every one checked
        against the disk as it is now. Nothing here writes."""
        for item_id in wanted:  # the old paths are looked at as strictly as the new ones
            validate_rel_path(records[item_id].rel_path)
        newly: dict[str, str] = {}
        for item_id, new in wanted.items():
            key = _fold(new)
            if key in newly:
                raise RenameRefusedError("destination_taken")
            newly[key] = item_id
        staying = {_fold(r.rel_path) for i, r in records.items() if i not in wanted}
        if any(key in staying for key in newly):
            raise RenameRefusedError("destination_taken")
        by_folder: dict[str, list[str]] = defaultdict(list)
        for item_id in wanted:
            by_folder[records[item_id].rel_path.split("/", 1)[0]].append(item_id)
        everything = defaultdict(list)
        for rec in records.values():
            everything[rec.rel_path.split("/", 1)[0]].append(rec.item_id)
        root = ctx.root_local or ""
        top = self._top_level(root)
        batches: list[list[PlannedRename]] = []
        for folder in sorted(by_folder):
            items = sorted(by_folder[folder], key=lambda i: records[i].rel_path)
            batch = self._batch(
                ctx, folder, items, records, wanted, everything[folder], top, covered
            )
            batches.append(batch)
        return [s for batch in batches for s in batch], batches

    def _top_level(self, root_local: str) -> dict[str, str]:
        try:
            return {_fold(name): name for name in self._fsops.list_dir(root_local)}
        except (FsopsError, OSError):
            return {}

    def _batch(
        self,
        ctx: DestinationContext,
        folder: str,
        items: list[str],
        records: dict[str, _Record],
        wanted: dict[str, str],
        in_folder: list[str],
        top: dict[str, str],
        covered: frozenset[str],
    ) -> list[PlannedRename]:
        news = {i: wanted[i] for i in items}
        targets = {n.split("/", 1)[0] for n in news.values()}
        whole = (
            len(items) == len(in_folder)
            and len(targets) == 1
            and next(iter(targets)) != folder
            and all("/" in n for n in news.values())
        )
        steps: list[PlannedRename] = []
        if whole:
            new_folder = next(iter(targets))
            self._free_folder(ctx, folder, new_folder, top)
            self._require_dir(ctx, folder)
            steps.append(PlannedRename("series_dir", folder, new_folder))
            for item in items:
                old_in_new = new_folder + records[item].rel_path[len(folder) :]
                if old_in_new != news[item]:
                    self._free_file(ctx, news[item], covered, vacated=set())
                    steps.append(PlannedRename("file", old_in_new, news[item], item))
        else:
            vacated = {records[i].rel_path for i in items}
            for item in items:
                self._require_file(ctx, records[item].rel_path)
                self._free_file(ctx, news[item], covered, vacated=vacated)
                steps.append(PlannedRename("file", records[item].rel_path, news[item], item))
        for step in steps:
            if within_any(ctx.local(step.src), covered) or within_any(ctx.local(step.dst), covered):
                raise RenameRefusedError("intent_open")
        return steps

    def _free_folder(
        self, ctx: DestinationContext, old: str, new: str, top: dict[str, str]
    ) -> None:
        taken = top.get(_fold(new))
        if taken is not None and taken != old:
            seen = self._fsops.observe(ctx.local(taken))
            raise RenameRefusedError(
                "not_a_folder" if seen is not None and seen[0] == "symlink" else "destination_taken"
            )
        if taken == old and new != old and _fold(new) != _fold(old):
            raise RenameRefusedError("destination_taken")
        seen = self._fsops.observe(ctx.local(new))
        if seen is not None:
            raise RenameRefusedError(
                "not_a_folder" if seen[0] == "symlink" else "destination_taken"
            )

    def _require_dir(self, ctx: DestinationContext, rel: str) -> None:
        seen = self._fsops.observe(ctx.local(rel))
        if seen is None or seen[0] != "dir":
            raise RenameRefusedError("not_a_folder")

    def _require_file(self, ctx: DestinationContext, rel: str) -> None:
        seen = self._fsops.observe(ctx.local(rel))
        if seen is None or seen[0] != "file":
            raise RenameRefusedError("not_a_file")

    def _free_file(
        self, ctx: DestinationContext, rel: str, covered: frozenset[str], *, vacated: set[str]
    ) -> None:
        """The target is free and every parent that exists is a real folder (a link is refused,
        never followed)."""
        parts = rel.split("/")
        for depth in range(1, len(parts)):
            seen = self._fsops.observe(ctx.local("/".join(parts[:depth])))
            if seen is None:
                break
            if seen[0] != "dir":
                raise RenameRefusedError("not_a_folder")
        if rel not in vacated and self._fsops.observe(ctx.local(rel)) is not None:
            raise RenameRefusedError("destination_taken")

    # -- the warning ------------------------------------------------------------------------

    def _warnings(
        self, ctx: DestinationContext, steps: Sequence[PlannedRename]
    ) -> tuple[ProgressLossWarning, ...]:
        folders = sorted({s.src.split("/", 1)[0] for s in steps})
        found = self._count_readers(ctx, folders)
        if found is None or (found.count == 0 and found.exact):
            return ()
        return (ProgressLossWarning(found.count, found.exact, tuple(folders)),)

    def _count_readers(self, ctx: DestinationContext, folders: Sequence[str]) -> ReaderCount | None:
        if self._readers is None:
            return None
        remote = [ctx.remote_folder(ctx.local(f)) for f in folders]
        return self._readers(remote)

    def require_confirmation(self, plan_id: str, *, confirmed: bool) -> None:
        """EF-EXP-037: asked before the plan is claimed, and again when it runs."""
        with self._engine.connect() as conn:
            plan = conn.execute(
                sa.select(coherence.export_plan).where(coherence.export_plan.c.plan_id == plan_id)
            ).one()
            ctx = load_context(conn, plan.shelf_destination_id)
            ops = (
                conn.execute(
                    sa.select(coherence.export_op.c.src_path).where(
                        coherence.export_op.c.plan_id == plan_id
                    )
                )
                .scalars()
                .all()
            )
        root = (ctx.root_local or "").rstrip("/") + "/"
        folders = sorted({p[len(root) :].split("/", 1)[0] for p in ops if p and p.startswith(root)})
        found = self._count_readers(ctx, folders)
        if found is None or confirmed:
            return
        if found.count > 0 or not found.exact:
            raise RenameConfirmationRequiredError(found.count, exact=found.exact)

    # -- the application --------------------------------------------------------------------

    def apply(
        self, plan_id: str, *, if_match: str, job_id: str,
        confirm_progress_loss: bool = False,
        versions: tuple[Callable[[], str], Callable[[], str]],
    ) -> RenameReport:  # fmt: skip
        """Apply a stored plan once. Nothing runs without the confirmation when readers would
        lose their progress; `StalePlanError` (412) when anything moved since the preview."""
        with self._engine.connect() as conn:
            plan = conn.execute(
                sa.select(coherence.export_plan).where(coherence.export_plan.c.plan_id == plan_id)
            ).one()
        if plan.status == "planned" and if_match == plan.base_version:
            self.require_confirmation(plan_id, confirmed=confirm_progress_loss)
        return self._run(plan_id, if_match=if_match, job_id=job_id, versions=versions)

    def resume(
        self, plan_id: str, *, job_id: str,
        versions: tuple[Callable[[], str], Callable[[], str]],
        settle: Callable[[], int],
    ) -> RenameReport:  # fmt: skip
        """Go on with a plan a stop cut short: the intents it left are settled first (the folder
        is at the source or at the target, never both), then the steps that are not done."""
        with self._engine.connect() as conn:
            status = conn.execute(
                sa.select(coherence.export_plan.c.status).where(
                    coherence.export_plan.c.plan_id == plan_id
                )
            ).scalar_one()
        if status != "applying":
            raise StalePlanError("the plan is not being applied")
        settled = settle()
        report = self._run(plan_id, if_match=None, job_id=job_id, versions=versions)
        return replace(report, settled=settled)

    def _run(
        self, plan_id: str, *, if_match: str | None, job_id: str,
        versions: tuple[Callable[[], str], Callable[[], str]],
    ) -> RenameReport:  # fmt: skip
        current, inputs = versions
        with self._engine.connect() as conn:
            plan = conn.execute(
                sa.select(coherence.export_plan).where(coherence.export_plan.c.plan_id == plan_id)
            ).one()
            ctx = load_context(conn, plan.shelf_destination_id)
        store = PlanStore(
            self._engine, clock=self._clock, current_version=current, step_version=inputs
        )
        journal = IntentJournal(self._engine, self._fsops, clock=self._clock)
        hook = self.settle_hook()
        done = {"n": 0}
        scanned: set[int] = set()
        batches_left = self._batch_sizes(plan_id)

        def run(op: PlanOp) -> None:
            payload = op.payload or {}
            held = self._hold(job_id, ctx, payload)
            with held:
                self._step(ctx, journal, hook, plan_id, op, payload)
            done["n"] += 1
            batches_left[op.batch] -= 1
            if batches_left[op.batch] == 0 and op.batch not in scanned:
                scanned.add(op.batch)
                self._finish_batch(ctx, plan_id, op.batch)

        if if_match is None:
            store.resume(plan_id, run=run)
        else:
            store.apply(plan_id, if_match=if_match, run=run)
        self._complete(ctx, plan_id)
        return RenameReport(plan_id, done["n"])

    def _batch_sizes(self, plan_id: str) -> dict[int, int]:
        table = coherence.export_op
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(table.c.batch, sa.func.count())
                .where(table.c.plan_id == plan_id, table.c.status == "pending")
                .group_by(table.c.batch)
            ).all()
        return {int(batch): int(count) for batch, count in rows}

    def _hold(
        self, job_id: str, ctx: DestinationContext, payload: dict[str, Any]
    ) -> AbstractContextManager[object]:
        folders = sorted(
            {
                str(payload[key]).split("/", 1)[0]
                for key in ("old_rel", "new_rel")
                if payload.get(key)
            }
        )
        return self._locks.hold_resources(
            job_id,
            [path_resource(f"{ctx.destination_id}/{f}") for f in folders],
            timeout=self._lock_wait_s,
        )

    def _step(
        self,
        ctx: DestinationContext,
        journal: IntentJournal,
        hook: Callable[[Connection, OpenIntent], None],
        plan_id: str,
        op: PlanOp,
        payload: dict[str, Any],
    ) -> None:
        src, dst = op.src, op.dst
        if src is None or dst is None:
            raise RenameRefusedError("invalid_path")
        scope = str(payload.get("scope"))
        extra = {k: v for k, v in payload.items() if k != "template"}
        extra["template_ref"] = self._template_ref(payload)
        if self._already_moved(ctx, payload):
            return  # the intent of a cut-short step was settled: the manifest says it is done
        try:
            if scope == "series_dir":
                self._make_parent(dst)
                journal.apply_move_dir(MoveDirStep(src, dst), payload=extra, plan_id=plan_id,
                                       on_done=hook)  # fmt: skip
            else:
                self._require_plain_file(src)
                self._make_parent(dst)
                journal.apply_rename(RenameStep(src, dst, payload=extra), on_done=hook)
        except MoveRefusedError as refused:
            raise RenameRefusedError(refused.reason) from None
        except FileExistsError:
            raise RenameRefusedError("destination_taken") from None
        except FileNotFoundError:
            raise RenameRefusedError("not_a_file") from None
        except FsopsError as error:
            raise RenameRefusedError("refused_by_the_disk") from error

    @staticmethod
    def _template_ref(payload: dict[str, Any]) -> str | None:
        text = payload.get("template")
        return NamingTemplate.loads(text).ref if text else None

    def _already_moved(self, ctx: DestinationContext, payload: dict[str, Any]) -> bool:
        """A step whose intent was settled by the boot: the records already say the new path."""
        scope = payload.get("scope")
        with self._engine.connect() as conn:
            table = export.export_record
            if scope == "file":
                found = conn.execute(
                    sa.select(table.c.rel_path).where(
                        table.c.shelf_destination_id == ctx.destination_id,
                        table.c.export_item_id == payload.get("item_id"),
                    )
                ).scalar()
                return found == payload.get("new_rel")
            old, new = str(payload.get("old_rel")), str(payload.get("new_rel"))
            rels = (
                conn.execute(
                    sa.select(table.c.rel_path).where(
                        table.c.shelf_destination_id == ctx.destination_id,
                        table.c.rel_path.isnot(None),
                        table.c.state.in_(_LIVE_STATES),
                    )
                )
                .scalars()
                .all()
            )
        under_old = [r for r in rels if r == old or r.startswith(old + "/")]
        under_new = [r for r in rels if r.startswith(new + "/")]
        return not under_old and bool(under_new)

    def _require_plain_file(self, local: str) -> None:
        seen = self._fsops.observe(local)
        if seen is None:
            raise FileNotFoundError(local)
        if seen[0] != "file":
            raise RenameRefusedError("not_a_file")

    def _make_parent(self, local: str) -> None:
        parent = posixpath.dirname(local)
        if self._fsops.observe(parent) is None:
            self._fsops.mkdir(parent, parents=True, exist_ok=True)
        elif self._fsops.observe(parent)[0] != "dir":  # type: ignore[index]
            raise RenameRefusedError("not_a_folder")

    def _finish_batch(self, ctx: DestinationContext, plan_id: str, batch: int) -> None:
        """A batch is done: the series folders follow their files, the folders it emptied go, and
        Kavita is asked for one scan of what changed."""
        table = coherence.export_op
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(table.c.src_path, table.c.dst_path).where(
                    table.c.plan_id == plan_id, table.c.batch == batch
                )
            ).all()
        root = (ctx.root_local or "").rstrip("/") + "/"
        self._sync_series_dirs(ctx)
        folders: set[str] = set()
        for src, dst in rows:
            self._remove_emptied(root, posixpath.dirname(src))
            for path in (src, dst):
                folders.add(path[len(root) :].split("/", 1)[0])
        if self._request_scan is not None:
            for folder in sorted(folders):
                if self._fsops.observe(ctx.local(folder)) is not None:
                    self._request_scan(ctx.remote_folder(ctx.local(folder)))

    def _remove_emptied(self, root: str, local: str) -> None:
        """A managed folder the batch emptied goes; the root and a folder with anything left
        (an unmanaged file, a link) never."""
        base = root.rstrip("/")
        while local.startswith(root) and local != base:
            try:
                seen = self._fsops.observe(local)
                if seen is None:
                    local = posixpath.dirname(local)
                    continue
                if seen[0] != "dir" or self._fsops.list_dir(local):
                    return
                self._fsops.rmdir(local)
            except (FsopsError, OSError):
                return
            local = posixpath.dirname(local)

    def _sync_series_dirs(self, ctx: DestinationContext) -> None:
        """`export_series.series_dir` follows its files when all of them are in one other folder."""
        record, link = export.export_record, export.edition_export_series
        item, series = export.export_item, export.export_series
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.select(link.c.export_series_id, record.c.rel_path)
                .select_from(
                    record.join(item, item.c.id == record.c.export_item_id).join(
                        link, link.c.edition_id == item.c.edition_id
                    )
                )
                .where(
                    record.c.shelf_destination_id == ctx.destination_id,
                    record.c.rel_path.isnot(None),
                    record.c.state.in_(_LIVE_STATES),
                    link.c.remote_library_id == ctx.remote_library_id,
                )
            ).all()
            tops: dict[str, set[str]] = defaultdict(set)
            for series_id, rel in rows:
                tops[series_id].add(rel.split("/", 1)[0])
            for series_id, folders in tops.items():
                if len(folders) == 1:
                    conn.execute(
                        sa.update(series)
                        .where(series.c.id == series_id, series.c.series_dir.isnot(None))
                        .values(series_dir=next(iter(folders)))
                    )

    def _complete(self, ctx: DestinationContext, plan_id: str) -> None:
        """The plan is done: a template change becomes the destination's own, so that the next
        export names with it (nothing is written a second time)."""
        with self._engine.connect() as conn:
            text = conn.execute(
                sa.select(coherence.export_op.c.payload)
                .where(coherence.export_op.c.plan_id == plan_id)
                .limit(1)
            ).scalar()
        template = (text or {}).get("template") if isinstance(text, dict) else None
        if template:
            parsed = NamingTemplate.loads(template)
            with self._engine.begin() as conn:
                conn.execute(
                    sa.update(export.shelf_destination)
                    .where(export.shelf_destination.c.id == ctx.destination_id)
                    .values(template=parsed.dumps(), template_origin=f"plan@{parsed.ref}")
                )

    # -- the bookkeeping of a step, in the transaction that closes its intent ----------------

    def settle_hook(self) -> Callable[[Connection, OpenIntent], None]:
        """For `IntentJournal.resolve_open(on_done=...)` and for the steps themselves: the
        manifest line, the replica and the template reference follow the file."""
        locate, fsops, clock = self._locate, self._fsops, self._clock

        def follow(connection: Connection, intent: OpenIntent) -> None:
            payload = intent.payload
            if intent.op != "rename" or payload.get("scope") not in (
                "file",
                "series_dir",
            ):
                return
            destination = str(payload["destination_id"])
            old, new = str(payload["old_rel"]), str(payload["new_rel"])
            ref = payload.get("template_ref")
            if payload["scope"] == "file":
                _follow_file(connection, fsops, locate, clock.now(), destination,
                             str(payload["item_id"]), old, new, ref)  # fmt: skip
            else:
                _follow_dir(connection, fsops, locate, clock.now(), destination, old, new, ref)

        return follow


def _follow_file(
    connection: Connection, fsops: Fsops, locate: Callable[[str], tuple[str, str]], now: datetime,
    destination: str, item_id: str, old: str, new: str, template_ref: object,
) -> None:  # fmt: skip
    record = export.export_record
    values: dict[str, Any] = {"rel_path": new, "updated_at": now}
    if template_ref:
        values["template_ref"] = template_ref
        item = export.export_item
        frozen = connection.execute(
            sa.select(item.c.frozen_attrs).where(item.c.id == item_id)
        ).scalar()
        if isinstance(frozen, dict):
            connection.execute(
                sa.update(item)
                .where(item.c.id == item_id)
                .values(frozen_attrs={**frozen, "template_ref": template_ref})
            )
    connection.execute(
        sa.update(record)
        .where(
            record.c.shelf_destination_id == destination,
            record.c.export_item_id == item_id,
            record.c.rel_path == old,
        )
        .values(**values)
    )
    _relocate_copy(connection, fsops, locate, now, destination, item_id, new)


def _follow_dir(
    connection: Connection, fsops: Fsops, locate: Callable[[str], tuple[str, str]], now: datetime,
    destination: str, old: str, new: str, template_ref: object,
) -> None:  # fmt: skip
    record = export.export_record
    rows = connection.execute(
        sa.select(record.c.export_item_id, record.c.rel_path).where(
            record.c.shelf_destination_id == destination, record.c.rel_path.isnot(None)
        )
    ).all()
    for item_id, rel in rows:
        if rel != old and not rel.startswith(old + "/"):
            continue
        moved = new + rel[len(old) :]
        connection.execute(
            sa.update(record)
            .where(record.c.shelf_destination_id == destination, record.c.export_item_id == item_id)
            .values(rel_path=moved, updated_at=now)
        )
        _relocate_copy(connection, fsops, locate, now, destination, item_id, moved)
    series = export.export_series
    connection.execute(
        sa.update(series)
        .where(series.c.shelf_destination_id == destination, series.c.series_dir == old)
        .values(series_dir=new)
    )


def _relocate_copy(
    connection: Connection, fsops: Fsops, locate: Callable[[str], tuple[str, str]], now: datetime,
    destination: str, item_id: str, rel: str,
) -> None:  # fmt: skip
    ctx = load_context(connection, destination)
    local = ctx.local(rel)
    seen = fsops.observe(local)
    if seen is None or seen[0] != "file":
        return
    root_id, root_rel = locate(local)
    identity = seen[1]
    relocate_destination_copy(
        connection, item_id, physical_root_id=root_id, rel_path=root_rel,
        dev=fsops.device_of(local), ino=identity.ino, size=identity.size,
        mtime_ns=identity.mtime_ns, verified_at=now,
    )  # fmt: skip
