"""Bring adopted files home before their mount goes (EF-EXP-051, EF-EXP-060).

The adopted files of a Kavita library may live outside `/library` (Suwayomi's download volume,
mounted in Kavita and, read-only, in Shelfhold: an `adopt_ro` root). Before that mount is
removed, each one is copied, verified by hash, to the **same relative path** under the writable
root of the destination. The path Kavita sees and the name of the series do not change; the
series, the chapters and the reading progress (Kavita's, found by path, and Shelfhold's, found
by `export_item` and `content_hash`) are therefore the same after.

The operation is only ever a copy: it reads an `adopt_ro` root and writes a `rw` one, through
the intent journal (`copy_cross_root`, EF-EXP-041), and it never deletes or moves anything, nor
overwrites a different file. It starts from a **dry-run**, whose fingerprint the apply must carry:
what is copied is what the admin looked at (`repatriate.stale` otherwise), and without it nothing
is done (`repatriate.dry_run_required`). A root restored behind (L35) is frozen by fsops like any
other write.
"""

import hashlib
import posixpath
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks, path_resource
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.domain.path_map import PathMode, clean_absolute
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import relocate_destination_copy

__all__ = [
    "DryRunRequiredError",
    "FileAction",
    "KavitaCounts",
    "NotEnoughSpaceError",
    "RepatriationBlockedError",
    "RepatriationCheck",
    "RepatriationPlan",
    "RepatriationResult",
    "RepatriationStaleError",
    "Repatriator",
    "kavita_counts",
]

Status = Literal["copy", "present", "missing", "changed", "conflict"]
BLOCKING: Final = frozenset({"missing", "changed", "conflict"})
SPACE_MARGIN: Final = 1.05  # the staged copy and the filesystem's own overhead
LOCK_WAIT_SECONDS: Final = 30.0


class DryRunRequiredError(Exception):
    """A repatriation without the fingerprint of a dry-run is not done."""

    code = "repatriate.dry_run_required"


class RepatriationStaleError(Exception):
    """What would be copied is not what the dry-run showed."""

    code = "repatriate.stale"

    def __init__(self, plan: "RepatriationPlan") -> None:
        super().__init__("the files changed since the dry-run")
        self.plan = plan


class RepatriationBlockedError(Exception):
    """A file cannot be brought home as it is: it is missing at the source, no longer the file
    that was adopted, or a different file is already at the target. Nothing was copied."""

    code = "repatriate.blocked"

    def __init__(self, problems: tuple["FileAction", ...]) -> None:
        super().__init__(f"{len(problems)} adopted file(s) cannot be repatriated")
        self.problems = problems


class NotEnoughSpaceError(Exception):
    code = "repatriate.no_space"

    def __init__(self, required: int, free: int) -> None:
        super().__init__(f"{required} bytes needed, {free} free")
        self.required = required
        self.free = free


@dataclass(frozen=True)
class FileAction:
    rel_path: str
    source: str
    target: str
    size: int
    file_hash: str
    export_item_id: str
    status: Status


@dataclass(frozen=True)
class RepatriationPlan:
    """The dry-run: counts, size, free space, and the fingerprint the apply must carry."""

    destination_id: str
    source_root: str
    target_root: str
    files: tuple[FileAction, ...]
    series: int
    chapters: int
    bytes_to_copy: int
    free_bytes: int
    fingerprint: str

    @property
    def to_copy(self) -> tuple[FileAction, ...]:
        return tuple(f for f in self.files if f.status == "copy")

    @property
    def problems(self) -> tuple[FileAction, ...]:
        return tuple(f for f in self.files if f.status in BLOCKING)

    @property
    def required_bytes(self) -> int:
        return int(self.bytes_to_copy * SPACE_MARGIN)

    @property
    def ready(self) -> bool:
        return not self.problems and self.free_bytes >= self.required_bytes


@dataclass(frozen=True)
class RepatriationResult:
    copied: int
    already_present: int
    bytes_copied: int
    series: int
    chapters: int
    intents: tuple[str, ...]


@dataclass(frozen=True)
class RepatriationCheck:
    """The control before the mount goes: every adopted file is under the writable root with the
    hash recorded at adoption, and the counts are the ones the dry-run gave."""

    series: int
    chapters: int
    files: int
    missing: tuple[str, ...]
    different: tuple[str, ...]

    @property
    def safe_to_unmount(self) -> bool:
        return not self.missing and not self.different


@dataclass(frozen=True)
class KavitaCounts:
    series: int
    files: int


@dataclass(frozen=True)
class _Adopted:
    rel_path: str
    size: int
    file_hash: str
    export_item_id: str
    units: tuple[str, ...]


class Repatriator:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        *,
        locate: Callable[[str], tuple[str, str]],
        clock: Clock,
        free_bytes: Callable[[str], int] | None = None,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._locate = locate
        self._clock = clock
        self._free = free_bytes or fsops.free_bytes
        self._journal = IntentJournal(engine, fsops, clock=clock)
        self._locks = ResourceLocks(engine, clock=clock)

    # -- the dry-run ------------------------------------------------------------------------

    def plan(self, destination_id: str, *, source: str | None = None) -> RepatriationPlan:
        """Read-only: nothing is written, in the database or on disk."""
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            adopted = self._adopted(connection, destination_id)
        if ctx.root_local is None:
            raise RepatriationBlockedError(())
        source_root = self._source_root(ctx.path_map.entries, ctx.root_subpath, source)
        target_root = ctx.root_local
        actions = tuple(self._action(source_root, target_root, item) for item in adopted)
        copy = sum(a.size for a in actions if a.status == "copy")
        folders = {posixpath.dirname(a.rel_path).split("/")[0] or a.rel_path for a in actions}
        chapters = {u for item in adopted for u in item.units}
        digest = hashlib.sha256(f"{source_root}\n{target_root}\n".encode())
        for a in sorted(actions, key=lambda a: a.rel_path):
            digest.update(f"{a.rel_path}|{a.file_hash}|{a.size}|{a.status}\n".encode())
        return RepatriationPlan(
            destination_id, source_root, target_root, actions, len(folders), len(chapters),
            copy, self._free(target_root), digest.hexdigest(),
        )  # fmt: skip

    @staticmethod
    def _source_root(entries: Any, root_subpath: str, source: str | None) -> str:
        """The `adopt_ro` entry of the target's path map; with several, `source` names one."""
        read_only = [e.local for e in entries if e.mode is PathMode.ADOPT_RO]
        if source is not None:
            source = clean_absolute(source)
            if source not in read_only:
                raise RepatriationBlockedError(())
            chosen = source
        elif len(read_only) == 1:
            chosen = read_only[0]
        else:
            raise RepatriationBlockedError(())
        return clean_absolute(f"{chosen}/{root_subpath}") if root_subpath else chosen

    def _action(self, source_root: str, target_root: str, item: _Adopted) -> FileAction:
        src, dst = f"{source_root}/{item.rel_path}", f"{target_root}/{item.rel_path}"

        def make(status: Status) -> FileAction:
            return FileAction(
                item.rel_path, src, dst, item.size, item.file_hash, item.export_item_id, status
            )

        at_target = self._fsops.observe(dst)
        if at_target is not None:
            same = at_target[0] == "file" and self._fsops.sha256_of(dst) == item.file_hash
            return make("present" if same else "conflict")
        at_source = self._fsops.observe(src)
        if at_source is None or at_source[0] != "file":
            return make("missing")
        return make("copy" if self._fsops.sha256_of(src) == item.file_hash else "changed")

    @staticmethod
    def _adopted(connection: Connection, destination_id: str) -> list[_Adopted]:
        record, coverage = export.export_record, export.unit_coverage
        rows = connection.execute(
            sa.select(record.c.rel_path, record.c.size, record.c.file_hash, record.c.export_item_id)
            .where(
                record.c.shelf_destination_id == destination_id,
                record.c.state == "adopted",
                record.c.locator_type == "path",
                record.c.file_hash.is_not(None),
            )
            .order_by(record.c.rel_path)
        ).all()
        units: dict[str, list[str]] = defaultdict(list)
        for c in connection.execute(
            sa.select(coverage.c.carrier_id, coverage.c.unit_id).where(
                coverage.c.carrier_type == "export_item",
                coverage.c.carrier_id.in_([r.export_item_id for r in rows]),
            )
        ):
            units[c.carrier_id].append(c.unit_id)
        return [
            _Adopted(
                r.rel_path, int(r.size or 0), r.file_hash, r.export_item_id,
                tuple(units[r.export_item_id]),
            )
            for r in rows
        ]  # fmt: skip

    # -- the copy ---------------------------------------------------------------------------

    def apply(
        self,
        destination_id: str,
        *,
        fingerprint: str | None,
        job_id: str,
        source: str | None = None,
    ) -> RepatriationResult:
        """Copy, verify, record. Without the fingerprint of a dry-run: refused. A file already
        there with the right hash is only recorded, so a run cut short is finished by the next."""
        if not fingerprint:
            raise DryRunRequiredError()
        plan = self.plan(destination_id, source=source)
        if plan.fingerprint != fingerprint:
            raise RepatriationStaleError(plan)
        if plan.problems:
            raise RepatriationBlockedError(plan.problems)
        if plan.free_bytes < plan.required_bytes:
            raise NotEnoughSpaceError(plan.required_bytes, plan.free_bytes)
        by_folder: dict[str, list[FileAction]] = defaultdict(list)
        for action in plan.files:
            by_folder[posixpath.dirname(action.rel_path)].append(action)
        copied = present = size = 0
        intents: list[str] = []
        for folder in sorted(by_folder):
            resource = path_resource(f"{destination_id}/{folder}")
            with self._locks.hold_resources(job_id, [resource], timeout=LOCK_WAIT_SECONDS) as held:
                for action in by_folder[folder]:
                    held.ensure()
                    if action.status == "copy":
                        intents.append(self._copy(action))
                        copied, size = copied + 1, size + action.size
                    else:
                        self._record(action, intent_id=None)
                        present += 1
        return RepatriationResult(copied, present, size, plan.series, plan.chapters, tuple(intents))

    def _copy(self, action: FileAction) -> str:
        source = self._fsops.observe(action.source)
        intent_id = self._journal.record(
            op="copy_cross_root", src=action.source, dst=action.target,
            expected_sha256=action.file_hash,
            expected_file_id=source[1] if source else None,
        )  # fmt: skip
        self._journal.mark_applying(intent_id)
        try:
            digest = self._fsops.copy_across(action.source, action.target, parents=True)
        except BaseException:
            self._journal.cancel(intent_id)  # atomic: nothing landed, the source is untouched
            raise
        if f"sha256:{digest}" != action.file_hash:
            self._journal.cancel(intent_id)
            raise RepatriationBlockedError((action,))
        self._record(action, intent_id=intent_id)
        return intent_id

    def _record(self, action: FileAction, *, intent_id: str | None) -> None:
        """The copy is the replica of the item, and the record knows its size and mtime (a
        foreign-writer check compares them): one transaction with the end of the intent."""
        found = self._fsops.observe(action.target)
        if found is None:
            raise RepatriationBlockedError((action,))
        identity = found[1]
        root_id, root_rel = self._locate(action.target)
        with self._engine.begin() as connection:
            relocate_destination_copy(
                connection, action.export_item_id, physical_root_id=root_id, rel_path=root_rel,
                dev=self._fsops.device_of(action.target), ino=identity.ino, size=identity.size,
                mtime_ns=identity.mtime_ns, verified_at=self._clock.now(),
            )  # fmt: skip
            connection.execute(
                sa.update(export.export_record)
                .where(export.export_record.c.export_item_id == action.export_item_id)
                .values(size=identity.size, mtime_ns=identity.mtime_ns)
            )
            if intent_id is not None:
                self._journal.mark_done(intent_id, within=connection)

    # -- the control before the mount goes --------------------------------------------------

    def check(self, destination_id: str) -> RepatriationCheck:
        """Every adopted file is under the writable root with the hash it had when it was
        adopted. Run it after the copy and again after the mount is removed."""
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            adopted = self._adopted(connection, destination_id)
        if ctx.root_local is None:
            raise RepatriationBlockedError(())
        missing: list[str] = []
        different: list[str] = []
        for item in adopted:
            found = self._fsops.observe(f"{ctx.root_local}/{item.rel_path}")
            if found is None:
                missing.append(item.rel_path)
            elif (
                found[0] != "file"
                or self._fsops.sha256_of(f"{ctx.root_local}/{item.rel_path}") != item.file_hash
            ):
                different.append(item.rel_path)
        folders = {posixpath.dirname(i.rel_path).split("/")[0] or i.rel_path for i in adopted}
        chapters = {u for i in adopted for u in i.units}
        return RepatriationCheck(
            len(folders), len(chapters), len(adopted), tuple(missing), tuple(different)
        )


async def kavita_counts(client: Any, library_ref: str, remote_root: str) -> KavitaCounts:
    """Series and files Kavita has indexed under `remote_root`: the same before the copy and after
    the mount is removed (read-only calls)."""
    root = clean_absolute(remote_root)
    series = [
        s
        for s in await client.series_of_library(int(library_ref))
        if (s.lowest_folder_path or s.folder_path).startswith(root)
    ]
    files = 0
    for s in series:
        files += len(await client.series_files(s.id))
    return KavitaCounts(len(series), files)
