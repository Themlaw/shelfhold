"""The executor of destination plans (M4 L04, EF-EXP-001, EF-EXP-058, EF-SRC-036, ADR-0052).

A destination **describes** (`FsOp` plan, `ApiCall`) and never touches a disk or a server. This is
the only place that runs what it described, and it trusts nothing of it:

- **A copy, never the object.** The plan a plugin hands over goes through `parse_destination_plan`
  (a JSON round trip, exact types, frozen models); the host runs that copy. An object the plugin
  keeps and edits afterwards changes nothing.
- **Checked right before each operation.** `check_destination_plan_before_run` (never the static
  check, which has no `planned`) runs on the copy for the whole plan, then again right before
  every operation. A copy that was altered after its validation is refused at the operation it
  was altered for: the operation is compared with what was validated, and checked again.
- **Planned for this call and this binding.** A `write` by reference cites an `export_item` the
  host planned (`PlannedLedger`) for **this** call and **this** binding, with the egress the
  instance attested. The same holds for `ApiCall.body`. A reference planned for another binding of
  the same server or for another server is refused. The bytes of an item that is not planned are
  never read.
- **Identity of the server.** Before anything else the fingerprint the server gives now is compared
  with the one stored (EF-EXP-039): a change pauses the target (`target.instance_changed`, health
  and event) and nothing is written until `target reconnect`.
- **A tree, one writer** (ADR-0052). The role and the folder come from the database, never from
  the caller: only the primary binding of a tree writes (T3); a tree that overlaps another is
  refused (`target.tree_overlap`), and so is a folder behind a symbolic link.
- **Every file operation is an intent** (EF-EXP-041, ADR-0031): the existing journal, `pending`,
  `applying`, the disk, `done`. A kill -9 leaves an intent the boot settles; the same call run
  again skips what is done by the intents it finds, never by looking at the disk.

Nothing of a call (query, body, field) is ever logged: no `model_dump` of an `ApiCall` here
(#275 m3), and a refusal names the rule, never a value.
"""

import hashlib
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.export.application.intents import IntentJournal, RenameStep
from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.target_state import (
    DestinationPausedError,
    ensure_active,
    load_fingerprint,
    load_health,
    save_fingerprint,
    save_health,
)
from shelfhold.export.domain.kavita_instance import instance_changed
from shelfhold.export.domain.trees import (
    TreeFolder,
    TreePathError,
    check_no_symlink,
    check_tree_attachment,
)
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.schema import coherence, export, platform
from shelfhold.sdk.api_call import ApiCall, check_api_call, parse_api_call
from shelfhold.sdk.export_target import TargetCapabilities
from shelfhold.sdk.fsops import (
    ContentRef,
    FsOp,
    FsOpPlan,
    FsOpPlanError,
    PlannedContent,
    check_destination_plan_before_run,
    parse_destination_plan,
)

__all__ = [
    "ContentStore",
    "ExecutionReport",
    "InstanceProbe",
    "PlanBinding",
    "PlanExecutor",
    "PlanRefusedError",
    "PlannedLedger",
]

FILE_TRANSPORTS: Final = ("tree", "dropbox")  # the ones that put files in a folder of the host


class PlanRefusedError(Exception):
    """The host will not run this plan, or this call. The reason names the rule, never a value."""

    code = "export.plan_refused"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ContentStore(Protocol):
    """The bytes the host owns, for one binding: only asked for what the ledger planned."""

    def read(self, destination_id: str, ref: ContentRef) -> bytes: ...


class InstanceProbe(Protocol):
    """What the server of a target says about its identity right now (EF-EXP-039); None when it
    gives none."""

    def fingerprint(self, target_id: str) -> str | None: ...


class PlannedLedger:
    """What the host planned, per call **and** binding. The planner records it; the executor
    asks for it and never builds it from what a plan says."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._planned: dict[tuple[str, str], PlannedContent] = {}

    def plan(self, call_id: str, destination_id: str, planned: PlannedContent) -> None:
        with self._lock:
            self._planned[(call_id, destination_id)] = planned

    def planned(self, call_id: str, destination_id: str) -> PlannedContent | None:
        with self._lock:
            return self._planned.get((call_id, destination_id))

    def release(self, call_id: str) -> None:
        with self._lock:
            for key in [k for k in self._planned if k[0] == call_id]:
                del self._planned[key]


@dataclass(frozen=True)
class PlanBinding:
    """The binding a plan is for. Only its identity and what the destination declares: the tree,
    its folder and the role of the binding in it are read from the database."""

    destination_id: str
    target_id: str
    capabilities: TargetCapabilities


@dataclass(frozen=True)
class ExecutionReport:
    executed: int
    skipped: int  # already done: by an intent of an earlier run, or already in place (a folder)


@dataclass(frozen=True)
class _Tree:
    local: str
    folder: TreeFolder
    role: str
    others: tuple[TreeFolder, ...]


@dataclass
class _Run:
    binding: PlanBinding
    call_id: str
    tree: _Tree
    plan: FsOpPlan
    planned: PlannedContent
    snapshot: tuple[str, ...]
    done: frozenset[int]
    executed: int = 0
    skipped: int = 0


class PlanExecutor:
    def __init__(
        self,
        engine: Engine,
        disk: Fsops,
        journal: IntentJournal,
        *,
        ledger: PlannedLedger,
        contents: ContentStore,
        probe: InstanceProbe,
        clock: Clock | None = None,
        events: TargetEvents | None = None,
    ) -> None:
        self._engine = engine
        self._disk = disk
        self._journal = journal
        self._ledger = ledger
        self._contents = contents
        self._probe = probe
        self._clock = clock or SystemClock()
        self._events = events

    # -- the entry points -----------------------------------------------------------------------

    def execute(self, binding: PlanBinding, call_id: str, raw_plan: object) -> ExecutionReport:
        """Run the plan a destination returned for one call, on one binding."""
        self._gate(binding)
        if binding.capabilities.transport not in FILE_TRANSPORTS:
            raise PlanRefusedError("this destination has no folder: it gets calls, not a file plan")
        tree = self._tree(binding)
        planned = self._planned(call_id, binding)
        plan = self._copy(raw_plan, planned)
        run = _Run(
            binding, call_id, tree, plan, planned,
            snapshot=tuple(op.model_dump_json() for op in plan.ops),
            done=self._settled(call_id, binding.destination_id),
        )  # fmt: skip
        for index in range(len(run.snapshot)):
            self._run_op(run, index)
        return ExecutionReport(run.executed, run.skipped)

    def run_calls(
        self,
        binding: PlanBinding,
        call_id: str,
        calls: Sequence[object],
        *,
        send: Callable[[ApiCall], object],
    ) -> int:
        """Send the calls a destination described, one by one: each is parsed into the host's own
        copy and checked right before it leaves. `send` is the Fetcher of the target (the core
        holds the secrets and the SSRF guard); it receives the copy and nothing else."""
        self._gate(binding)
        planned = self._planned(call_id, binding)
        sent = 0
        for raw in calls:
            try:
                call = parse_api_call(raw)
                check_api_call(call, planned, can_purge=binding.capabilities.can_purge)
            except (FsOpPlanError, ValueError, TypeError):
                raise PlanRefusedError("a call the host will not send") from None
            send(call)
            sent += 1
        return sent

    # -- before anything is touched -------------------------------------------------------------

    def _gate(self, binding: PlanBinding) -> None:
        """A paused binding waits; a server that is not the one the host knew pauses it."""
        with self._engine.connect() as connection:
            ensure_active(connection, binding.destination_id)
            stored = load_fingerprint(connection, binding.target_id)
        current = self._probe.fingerprint(binding.target_id)
        if stored is None:
            if current is not None:  # a first sight is not a change
                with write_transaction(self._engine) as connection:
                    save_fingerprint(
                        connection, binding.target_id, current, None, self._clock.now()
                    )
            return
        if not instance_changed(stored, current):
            return
        with write_transaction(self._engine) as connection:
            health = load_health(connection, binding.target_id)
            first = not health.instance_changed
            health.instance_changed = True
            health.refresh_waiting()
            save_health(connection, binding.target_id, health)
        if first and self._events is not None:
            self._events.emit("target.instance_changed", target_id=binding.target_id)
        raise DestinationPausedError("target.instance_changed")

    def _planned(self, call_id: str, binding: PlanBinding) -> PlannedContent:
        planned = self._ledger.planned(call_id, binding.destination_id)
        if planned is None:
            raise PlanRefusedError("nothing is planned for this call and this binding")
        return planned

    def _tree(self, binding: PlanBinding) -> _Tree:
        """The folder and the role of the binding, as the database holds them, and the checks of
        ADR-0052: one writer (the primary), no overlap with another tree, no link on the way."""
        with self._engine.connect() as connection:
            tree = _load_tree(connection, binding)
        if tree is None:
            raise PlanRefusedError("this binding has no tree")
        if tree.role != "primary":
            raise PlanRefusedError("an indexer never writes into the tree: the primary does")
        check_tree_attachment(tree.folder, tree.others)  # `TreeOverlapError`: target.tree_overlap
        root = (
            tree.local.removesuffix("/" + tree.folder.subpath)
            if tree.folder.subpath
            else tree.local
        )

        def kind_of(rel: str) -> str | None:
            try:
                found = self._disk.observe(f"{root}/{rel}")
            except (FsopsError, OSError):
                return "symlink"  # a path the file system will not let us look at is not safe
            return found[0] if found else None

        try:
            check_no_symlink(tree.folder.subpath, kind_of)
        except TreePathError:
            raise PlanRefusedError("the folder of a tree never goes through a link") from None
        return tree

    def _copy(self, raw_plan: object, planned: PlannedContent) -> FsOpPlan:
        try:
            plan = parse_destination_plan(raw_plan)
            check_destination_plan_before_run(plan, planned=planned)
        except (FsOpPlanError, ValueError, TypeError):
            raise PlanRefusedError("the plan is not one the host will run") from None
        return plan

    def _settled(self, call_id: str, destination_id: str) -> frozenset[int]:
        """The operations an earlier run of this call finished, by their intents. An intent that is
        still open stops the run: the boot settles it first, nothing is replayed blind."""
        table = coherence.fs_intent
        call = sa.func.json_extract(table.c.expected_file_id, "$.exec")
        dest = sa.func.json_extract(table.c.expected_file_id, "$.dest")
        index = sa.func.json_extract(table.c.expected_file_id, "$.op_index")
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(table.c.state, index.label("i")).where(
                    call == call_id,
                    dest == destination_id,
                    table.c.state.in_(("pending", "applying", "done")),
                )
            ).all()
        if any(r.state != "done" for r in rows):
            raise PlanRefusedError("an earlier run of this call is not settled yet")
        return frozenset(int(r.i) for r in rows if r.i is not None)

    # -- one operation --------------------------------------------------------------------------

    def _run_op(self, run: _Run, index: int) -> None:
        op = self._checked(run, index)
        if index in run.done:
            run.skipped += 1
            return
        where = f"{run.tree.local}/{op.rel_path}"
        meta = {"exec": run.call_id, "dest": run.binding.destination_id, "op_index": index}
        match op.op:
            case "mkdir":
                self._mkdir(run, where)
            case "write" | "write_inline":
                self._write(run, op, where, meta)
            case "rename":
                self._rename(run, op, where, meta)
            case "recycle":
                self._recycle(where, meta)
                run.executed += 1
            case "touch_mtime":
                self._touch(op, where, meta)
                run.executed += 1
            case _:  # `unlink` and the like never reach here: the check refuses them
                raise PlanRefusedError("an operation the host does not run for a destination")

    def _checked(self, run: _Run, index: int) -> FsOp:
        """The operation as the copy holds it **now**, checked right before it runs, and equal to
        what the whole plan was validated with."""
        op = run.plan.ops[index]
        try:
            check_destination_plan_before_run(
                FsOpPlan.model_construct(ops=(op,)), planned=run.planned
            )
            same = type(op) is FsOp and op.model_dump_json() == run.snapshot[index]
        except (FsOpPlanError, ValueError, TypeError, AttributeError):
            raise PlanRefusedError("an operation changed after the plan was validated") from None
        if not same:
            raise PlanRefusedError("an operation changed after the plan was validated")
        return op

    def _mkdir(self, run: _Run, where: str) -> None:
        found = self._disk.observe(where)
        if found is not None and found[0] == "dir":
            run.skipped += 1
            return
        self._disk.mkdir(where, parents=True, exist_ok=True)
        run.executed += 1

    def _write(self, run: _Run, op: FsOp, where: str, meta: dict[str, Any]) -> None:
        if op.content_ref is None:
            raise PlanRefusedError("a write cites the bytes it writes")
        if self._disk.observe(where) is not None:
            raise PlanRefusedError("the path is taken by a file the host did not write")
        data = self._contents.read(run.binding.destination_id, op.content_ref)
        intent = self._journal.record(
            op="write", dst=where, expected_sha256=_digest(data), extra=meta
        )
        self._journal.mark_applying(intent)
        try:
            self._disk.write_atomic(
                where, data, parents=True, replace=False, mtime_ns=_ns(op.mtime)
            )
        except (OSError, FsopsError):
            self._journal.cancel(intent)
            raise PlanRefusedError("the disk refused the write") from None
        self._journal.mark_done(intent)
        run.executed += 1

    def _rename(self, run: _Run, op: FsOp, where: str, meta: dict[str, Any]) -> None:
        if op.dst_rel_path is None:
            raise PlanRefusedError("a rename needs its target")
        target = f"{run.tree.local}/{op.dst_rel_path}"
        if self._disk.observe(target) is not None:
            raise PlanRefusedError("a rename never overwrites")
        try:
            self._journal.apply_rename(
                RenameStep(src=where, dst=target, payload=meta), cancel_on_refusal=True
            )
        except (OSError, FsopsError):
            raise PlanRefusedError("the disk refused the rename") from None
        run.executed += 1

    def _recycle(self, where: str, meta: dict[str, Any]) -> None:
        found = self._disk.observe(where)
        if found is None or found[0] != "file":
            raise PlanRefusedError("only a file is recycled")
        intent = self._journal.record(
            op="recycle", src=where, expected_sha256=self._disk.sha256_of(where), extra=meta
        )
        self._journal.mark_applying(intent)
        try:
            self._disk.recycle(where)
        except (OSError, FsopsError):
            self._journal.cancel(intent)
            raise PlanRefusedError("the disk refused the move to the bin") from None
        self._journal.mark_done(intent)

    def _touch(self, op: FsOp, where: str, meta: dict[str, Any]) -> None:
        """Set the mtime of a file: the file is written again, byte for byte, with that mtime
        (the façade has no `utime`). A crash leaves the file as it was; the intent settles as done
        and the mtime is then the only thing lost, which is cosmetic."""
        found = self._disk.observe(where)
        if found is None or found[0] != "file":
            raise PlanRefusedError("only a file gets a new mtime")
        data = self._disk.read_bytes(where)
        intent = self._journal.record(
            op="write", dst=where, expected_sha256=_digest(data), extra=meta
        )
        self._journal.mark_applying(intent)
        try:
            self._disk.write_atomic(where, data, replace=True, mtime_ns=_ns(op.mtime))
        except (OSError, FsopsError):
            self._journal.cancel(intent)
            raise PlanRefusedError("the disk refused the change of mtime") from None
        self._journal.mark_done(intent)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _ns(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return int(aware.timestamp()) * 1_000_000_000 + aware.microsecond * 1_000


def _load_tree(connection: Connection, binding: PlanBinding) -> _Tree | None:
    tree, indexer, root = export.tree, export.tree_indexer, platform.physical_root
    row = connection.execute(
        sa.select(tree.c.id, tree.c.root_id, tree.c.root_subpath, indexer.c.role, root.c.path_entry)
        .select_from(
            indexer.join(tree, tree.c.id == indexer.c.tree_id).join(
                root, root.c.root_id == tree.c.root_id
            )
        )
        .where(
            indexer.c.shelf_destination_id == binding.destination_id,
            indexer.c.target_id == binding.target_id,
        )
    ).first()
    if row is None:
        return None
    others = tuple(
        TreeFolder(root_id=r.root_id, subpath=r.root_subpath, tree_id=r.id)
        for r in connection.execute(
            sa.select(tree.c.id, tree.c.root_id, tree.c.root_subpath).where(tree.c.id != row.id)
        )
    )
    local = (
        f"{row.path_entry.rstrip('/')}/{row.root_subpath}" if row.root_subpath else row.path_entry
    )
    folder = TreeFolder(root_id=row.root_id, subpath=row.root_subpath, tree_id=row.id)
    return _Tree(local, folder, row.role, others)
