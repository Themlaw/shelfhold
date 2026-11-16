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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
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
from shelfhold.export.domain.folders import FOLDER_TRANSPORTS, fold
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
    "Barrier",
    "ContentStore",
    "ExecutionReport",
    "InstanceProbe",
    "PlanBinding",
    "PlanExecutor",
    "PlanRefusedError",
    "PlannedLedger",
    "WrittenFile",
]

FILE_TRANSPORTS: Final = FOLDER_TRANSPORTS  # one list with `check_binding`: the folder transports


class PlanRefusedError(Exception):
    """The host will not run this plan, or this call. The reason names the rule, never a value."""

    code = "export.plan_refused"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Barrier(Protocol):
    """What is looked at again while a plan runs (a shelf below 18+: the partitioning of the
    tree, the ceiling). `holds` is False when what was planned may no longer be written."""

    def holds(self, destination_id: str, planned: PlannedContent) -> bool: ...


class ContentStore(Protocol):
    """The bytes the host owns, for one binding: only asked for what the ledger planned."""

    def read(self, destination_id: str, ref: ContentRef) -> bytes: ...


class InstanceProbe(Protocol):
    """What the server of a target says about its identity right now (EF-EXP-039); None when it
    gives none."""

    def fingerprint(self, target_id: str) -> str | None: ...


class PlannedLedger:
    """What the host planned, per call **and** binding. The planner records it; the executor
    asks for it and never builds it from what a plan says. `sidecars` are the generated annexes
    (kind `sidecar`) the host made for this call and this binding: a sidecar that is not there is
    never read."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._planned: dict[tuple[str, str], tuple[PlannedContent, frozenset[str]]] = {}

    def plan(
        self,
        call_id: str,
        destination_id: str,
        planned: PlannedContent,
        *,
        sidecars: frozenset[str] = frozenset(),
    ) -> None:
        with self._lock:
            self._planned[(call_id, destination_id)] = (planned, sidecars)

    def planned(self, call_id: str, destination_id: str) -> PlannedContent | None:
        with self._lock:
            found = self._planned.get((call_id, destination_id))
        return found[0] if found else None

    def sidecars(self, call_id: str, destination_id: str) -> frozenset[str]:
        with self._lock:
            found = self._planned.get((call_id, destination_id))
        return found[1] if found else frozenset()

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
class WrittenFile:
    """A file of the plan that is on the disk now, by the bytes of an `export_item`: what the
    manifest of the tree is made from. Listed whether this run wrote it or an earlier one did."""

    export_item_id: str
    rel_path: str
    file_hash: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ExecutionReport:
    executed: int
    skipped: int  # already done: by an intent of an earlier run, or already in place (a folder)
    files: tuple[WrittenFile, ...] = ()
    moved: tuple[tuple[str, str], ...] = ()  # `(from, to)` rel paths the plan renamed
    recycled: tuple[
        str, ...
    ] = ()  # rel paths the plan sent to the bin (or moved away, see `_write`)


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
    keys: tuple[str, ...]
    done: frozenset[str]
    sidecars: frozenset[str]
    executed: int = 0
    skipped: int = 0
    since_probe: int = 0
    files: list[WrittenFile] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    recycled: list[str] = field(default_factory=list)


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
        recheck_every: int = 1,
        barrier: Barrier | None = None,
    ) -> None:
        self._barrier = barrier
        self._recheck_every = max(1, recheck_every)
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
        self._confirm(binding)
        self._gate(binding)
        if binding.capabilities.transport not in FILE_TRANSPORTS:
            raise PlanRefusedError("this destination has no folder: it gets calls, not a file plan")
        tree = self._tree(binding)
        planned = self._planned(call_id, binding)
        plan = self._copy(raw_plan, planned)
        snapshot = tuple(op.model_dump_json() for op in plan.ops)
        run = _Run(
            binding, call_id, tree, plan, planned,
            snapshot=snapshot,
            keys=_keys(snapshot),
            done=self._settled(call_id, binding.destination_id),
            sidecars=self._ledger.sidecars(call_id, binding.destination_id),
        )  # fmt: skip
        for op in plan.ops:
            self._screen(run, op)  # every operation, before the first one runs
        for index in range(len(run.snapshot)):
            self._run_op(run, index)
        return ExecutionReport(
            run.executed, run.skipped, tuple(run.files), tuple(run.moved), tuple(run.recycled)
        )

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
        self._confirm(binding)
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

    def _confirm(self, binding: PlanBinding) -> None:
        """The target and the transport of the binding are the ones the database holds for that
        binding, never the ones the caller says: the identity that is checked is the identity of
        the server this binding writes to."""
        table = export.shelf_destination
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(table.c.target_id, table.c.transport).where(
                    table.c.id == binding.destination_id
                )
            ).first()
        if (
            row is None
            or row.target_id != binding.target_id
            or row.transport != binding.capabilities.transport
        ):
            raise PlanRefusedError("the binding is not the one the host holds")

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
        ADR-0052: one writer (the primary), no overlap with another tree (by name, and by what the
        disk says: one folder reached through two roots is one tree), no link on the way."""
        with self._engine.connect() as connection:
            loaded = _load_trees(connection, binding)
        if loaded is None:
            raise PlanRefusedError("this binding has no tree")
        mine, role, others = loaded
        if role != "primary":
            raise PlanRefusedError("an indexer never writes into the tree: the primary does")
        folder = self._with_identity(mine)
        check_tree_attachment(folder, [self._with_identity(o) for o in others])
        root = mine.root_path

        def kind_of(rel: str) -> str | None:
            try:
                found = self._disk.observe(f"{root}/{rel}")
            except (FsopsError, OSError):
                return "symlink"  # a path the file system will not let us look at is not safe
            return found[0] if found else None

        try:
            check_no_symlink(folder.subpath, kind_of)
        except TreePathError:
            raise PlanRefusedError("the folder of a tree never goes through a link") from None
        return _Tree(mine.local, folder, role, ())

    def _with_identity(self, located: "_Located") -> TreeFolder:
        """The folder with `(st_dev, st_ino)` of itself and of every folder down to it. The inode
        only serves the overlap check, it is never stored (`st_dev` changes at every boot)."""
        parts = located.subpath.split("/") if located.subpath else []
        seen: list[tuple[int, int]] = []
        last: tuple[int, int] | None = None
        for depth in range(len(parts) + 1):
            path = "/".join([located.root_path, *parts[:depth]])
            try:
                stat = self._disk.link_stat(path)
            except (FsopsError, OSError):
                last = None  # not there yet (or not a path the façade looks at): no identity
                if depth > 0:
                    break
                continue
            last = (stat[0], stat[1])
            seen.append(last)
        return TreeFolder(
            root_id=located.root_id,
            subpath=located.subpath,
            tree_id=located.tree_id,
            identity=last,
            ancestry=frozenset(seen),
        )

    def _copy(self, raw_plan: object, planned: PlannedContent) -> FsOpPlan:
        try:
            plan = parse_destination_plan(raw_plan)
            check_destination_plan_before_run(plan, planned=planned)
        except (FsOpPlanError, ValueError, TypeError):
            raise PlanRefusedError("the plan is not one the host will run") from None
        return plan

    def _settled(self, call_id: str, destination_id: str) -> frozenset[str]:
        """The operations an earlier run of this call finished, by the **key** of each (its type,
        its target, its content reference: see `_keys`), never by its place in a plan: a plan that
        was made again redoes what is missing. An intent that is still open stops the run: the
        boot settles it first, nothing is replayed blind."""
        table = coherence.fs_intent
        call = sa.func.json_extract(table.c.expected_file_id, "$.exec")
        dest = sa.func.json_extract(table.c.expected_file_id, "$.dest")
        key = sa.func.json_extract(table.c.expected_file_id, "$.op_key")
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(table.c.state, key.label("k")).where(
                    call == call_id,
                    dest == destination_id,
                    table.c.state.in_(("pending", "applying", "done")),
                )
            ).all()
        if any(r.state != "done" for r in rows):
            raise PlanRefusedError("an earlier run of this call is not settled yet")
        return frozenset(str(r.k) for r in rows if r.k is not None)

    # -- one operation --------------------------------------------------------------------------

    def _run_op(self, run: _Run, index: int) -> None:
        op = self._checked(run, index)
        where = f"{run.tree.local}/{op.rel_path}"
        if run.keys[index] in run.done:
            run.skipped += 1
            self._note(run, op, where)  # done by an earlier run: still part of the manifest
            return
        if op.op != "mkdir":
            self._probe_again(run)
        meta = {"exec": run.call_id, "dest": run.binding.destination_id, "op_key": run.keys[index]}
        match op.op:
            case "mkdir":
                self._mkdir(run, where)
            case "write" | "write_inline":
                self._write(run, op, where, meta)
            case "rename":
                self._rename(run, op, where, meta)
                self._note(run, op, where)
            case "recycle":
                self._recycle(where, meta)
                run.executed += 1
                self._note(run, op, where)
            case "touch_mtime":
                self._touch(op, where, meta)
                run.executed += 1
            case _:  # `unlink` and the like never reach here: the check refuses them
                raise PlanRefusedError("an operation the host does not run for a destination")

    def _probe_again(self, run: _Run) -> None:
        """The identity of the server is read again before an operation that changes a file, every
        `recheck_every` of them (1 by default): a server replaced in the middle of a plan of ten
        thousand operations never receives the rest of it. The probe of production bounds its own
        cost (a cached answer, a few seconds old at most), not the executor."""
        run.since_probe += 1
        if run.since_probe >= self._recheck_every:
            run.since_probe = 0
            self._gate(run.binding)
            if self._barrier is not None and not self._barrier.holds(
                run.binding.destination_id, run.planned
            ):
                raise PlanRefusedError("the barrier of the shelf no longer holds")

    def _screen(self, run: _Run, op: FsOp) -> None:
        """What the SDK check does not know: a sidecar the host did not make for this call and
        this binding is never read, and a file the destination declares harmful (`series.json`
        for Kavita) is never written, whatever bytes it cites."""
        ref = op.content_ref
        if ref is not None and ref.kind == "sidecar" and ref.ref not in run.sidecars:
            raise PlanRefusedError("the sidecar is not one the host made for this binding")
        forbidden = {fold(name) for name in run.binding.capabilities.forbidden_sidecars}
        for created in (op.rel_path, op.dst_rel_path) if op.op in _CREATES else ():
            if created is not None and fold(PurePosixPath(created).name) in forbidden:
                raise PlanRefusedError("this destination does not take this sidecar")

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
        self._screen(run, op)
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
        self._move_away_old(run, op, meta)
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
        self._note(run, op, where)

    def _move_away_old(self, run: _Run, op: FsOp, meta: dict[str, Any]) -> None:
        """An item the manifest of this binding already holds at another path is moved, never
        copied: the old file goes to the bin under an intent of its own before the new one is
        written, so no copy exists that the manifest does not know. Only what the host wrote, and
        still the file the manifest names (a file that changed is left, and the write refused)."""
        ref = op.content_ref
        if ref is None or ref.kind != "export_item":
            return
        record = export.export_record
        with self._engine.connect() as connection:
            line = connection.execute(
                sa.select(record.c.rel_path, record.c.file_hash).where(
                    record.c.shelf_destination_id == run.binding.destination_id,
                    record.c.export_item_id == ref.ref,
                    record.c.state.in_(("written", "scanned", "resolved")),
                    record.c.rel_path.is_not(None),
                )
            ).first()
        if line is None or line.rel_path == op.rel_path:
            return
        old = f"{run.tree.local}/{line.rel_path}"
        found = self._disk.observe(old)
        if found is None or found[0] != "file":
            return  # already gone (an earlier run of this very move): the new file may be written
        if line.file_hash and self._disk.sha256_of(old) != line.file_hash:
            raise PlanRefusedError("the file the manifest names changed since it was written")
        self._recycle(old, {**meta, "op_key": meta["op_key"] + ":move"})
        run.recycled.append(line.rel_path)

    def _note(self, run: _Run, op: FsOp, where: str) -> None:
        """A file the plan put there by an item of the host: kept for the manifest of the tree. The
        hash is the one of the disk now, never one a plan said."""
        if op.op == "rename" and op.dst_rel_path is not None:
            run.moved.append((op.rel_path, op.dst_rel_path))
            return
        if op.op == "recycle":
            run.recycled.append(op.rel_path)
            return
        ref = op.content_ref
        if op.op != "write" or ref is None or ref.kind != "export_item":
            return
        found = self._disk.observe(where)
        if found is None or found[0] != "file":
            return  # moved or binned since: the manifest follows what is there
        digest = self._disk.sha256_of(where)  # `sha256:<hex>`
        run.files.append(
            WrittenFile(ref.ref, op.rel_path, digest, found[1].size, found[1].mtime_ns)
        )

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
        """Set the mtime of a file **in place** (`Fsops.utime`): the inode, its hard links, its
        owner and its mode stay, and a file of an adoption root is refused by the façade. An
        intent of its own: the boot sets the time again, which is the same whenever the crash
        came."""
        found = self._disk.observe(where)
        if found is None or found[0] != "file" or op.mtime is None:
            raise PlanRefusedError("only a file gets a new mtime")
        mtime_ns = _ns(op.mtime)
        assert mtime_ns is not None  # noqa: S101 - `op.mtime` is set, checked just above
        intent = self._journal.record(
            op="utime",
            dst=where,
            expected_sha256=self._disk.sha256_of(where),
            extra={**meta, "mtime_ns": mtime_ns},
        )
        self._journal.mark_applying(intent)
        try:
            self._disk.utime(where, mtime_ns=mtime_ns)
        except (OSError, FsopsError):
            self._journal.cancel(intent)
            raise PlanRefusedError("the disk refused the change of mtime") from None
        self._journal.mark_done(intent)


_CREATES: Final = ("write", "write_inline", "rename")  # operations that put a name on the disk


def _keys(snapshot: tuple[str, ...]) -> tuple[str, ...]:
    """The stable key of each operation: a digest of its normalised JSON (type, target, content
    reference, time) and of its rank among identical operations of the plan, so that a plan made
    again keeps the keys of what it still asks for."""
    seen: dict[str, int] = {}
    keys = []
    for text in snapshot:
        seen[text] = seen.get(text, 0) + 1
        keys.append(hashlib.sha256(f"{text}#{seen[text]}".encode()).hexdigest())
    return tuple(keys)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _ns(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return int(aware.timestamp()) * 1_000_000_000 + aware.microsecond * 1_000


@dataclass(frozen=True)
class _Located:
    tree_id: str
    root_id: str
    root_path: str
    subpath: str

    @property
    def local(self) -> str:
        return f"{self.root_path}/{self.subpath}" if self.subpath else self.root_path


def _load_trees(
    connection: Connection, binding: PlanBinding
) -> tuple[_Located, str, list[_Located]] | None:
    tree, indexer, root = export.tree, export.tree_indexer, platform.physical_root
    columns = (tree.c.id, tree.c.root_id, tree.c.root_subpath, root.c.path_entry)
    joined = tree.join(root, root.c.root_id == tree.c.root_id)
    row = connection.execute(
        sa.select(*columns, indexer.c.role)
        .select_from(joined.join(indexer, indexer.c.tree_id == tree.c.id))
        .where(
            indexer.c.shelf_destination_id == binding.destination_id,
            indexer.c.target_id == binding.target_id,
        )
    ).first()
    if row is None:
        return None

    def located(r: Any) -> _Located:
        return _Located(r.id, r.root_id, r.path_entry.rstrip("/"), r.root_subpath)

    others = [
        located(r)
        for r in connection.execute(
            sa.select(*columns).select_from(joined).where(tree.c.id != row.id)
        )
    ]
    return located(row), row.role, others
