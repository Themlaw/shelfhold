"""Removing a destination: keep the files and forget the manifest, or take away everything
Shelfhold wrote there (M3 L30, EF-EXP-031).

Two ways, and the warning is read before either:

- **keep the files** (`KEEP_FILES`): the folder is left exactly as it is, Kavita keeps its library;
  Shelfhold forgets the manifest, the series it kept for the folder and the destination itself.
  Nothing is written on any disk;
- **everything** (`DELETE_FILES`): every file *Shelfhold wrote* is deleted for good, past the bin.
  A volume the admin already had (an adopted one) is never Shelfhold's to delete and stays.

Both are one reserved action (`destination.remove_all`): a fresh admin session or the host CLI,
never a key, a member or a child, and the admin types the phrase `remove <destination id>`. The
check is made here, not only at the door.

The safety is the order, one file at a time, each step durable before the next:

1. **the intention** (`fs_intent`, op `unlink`, naming the destination), committed;
2. it is marked `applying`;
3. the file goes through `Fsops.purge_unlink` (one regular file: never a link, never a folder,
   never a host file, never out of the root);
4. one transaction closes the intention and forgets the file's manifest line and replica.

A stop anywhere leaves an intention the same command finishes (`apply` again): a file that is gone
closes its line, a file still there goes on. Or `settle(resume=False)` gives up: the intentions of
this destination that did not reach the disk are cancelled, and the manifest says what the disk
says. A file that cannot be removed (out of the root, a link, changed since it was exported) is
**left and reported**, and the destination is not forgotten until nothing is left.
"""

import hmac
import posixpath
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.export.application.intents import (
    IntentJournal,
    OpenIntent,
    holds_any,
    open_intent_paths,
    within_any,
)
from shelfhold.export.application.locks import LockBusyError, ResourceLocks, dest_resource
from shelfhold.export.application.root_ref import CopyScope, RootDirectory, copy_scope
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.path_map import PathMapError, clean_absolute, within
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    AuditEntry,
    Principal,
    audit_insert,
    authorize_reserved,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import FileChangedError, FileIdentity, Fsops, FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import forget_destination_copies

__all__ = [
    "REMOVAL_SCOPE",
    "LeftLine",
    "RemovalMode",
    "RemovalPreview",
    "RemovalRefusedError",
    "RemovalReport",
    "RemovalWarning",
    "RemoveDestination",
    "forget_destination",
    "removal_phrase",
]

log = structlog.get_logger()

REMOVAL_SCOPE = "remove_destination"  # what the intentions of this operation say about themselves
_ACTION = "destination.remove_all"


class RemovalMode(StrEnum):
    KEEP_FILES = "keep_files"
    DELETE_FILES = "delete_files"


def removal_phrase(destination_id: str) -> str:
    """What the admin types: the id, so that a pasted command names its destination."""
    return f"remove {destination_id}"


class RemovalRefusedError(PermissionError):
    """Nothing was changed. `reason`: `not_allowed` (the reserved action refused the caller),
    `confirmation_required` (the phrase was not typed), `intent_open` (an open file operation
    covers the destination's files, I9), `busy` (another job holds the destination), `no_folder`
    (the destination has no folder on this machine), `unknown_destination`."""

    def __init__(self, reason: str, *, verdict: object | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.verdict = verdict


@dataclass(frozen=True)
class RemovalWarning:
    code: str
    count: int = 0


@dataclass(frozen=True)
class RemovalPreview:
    destination_id: str
    mode: RemovalMode
    warnings: tuple[RemovalWarning, ...]
    phrase: str


@dataclass(frozen=True)
class LeftLine:
    """A file that stays, and why: `outside_root`, `not_a_regular_file`, `changed_since_export`,
    `refused_by_the_disk`, `no_hash` (the manifest has no hash for it: nothing says it is
    Shelfhold's own). The path is the manifest's (relative to the destination), never an
    absolute one."""

    code: str
    path: str


@dataclass
class RemovalReport:
    deleted: int = 0
    forgotten: int = 0  # manifest lines forgotten
    cancelled: int = 0  # intentions given up by `settle(resume=False)`
    left: tuple[LeftLine, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return not self.left


class RemoveDestination:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        journal: IntentJournal,
        *,
        clock: Clock,
        roots: RootDirectory,
        locks: ResourceLocks | None = None,
        request_scan: Callable[[str], None] | None = None,
        fresh_auth_s: int = 900,
    ) -> None:
        self._engine = engine
        self._roots = roots
        self._fsops = fsops
        self._journal = journal
        self._clock = clock
        self._locks = locks
        self._request_scan = request_scan
        self._fresh_auth_s = fresh_auth_s

    # -- the warning ----------------------------------------------------------------------------

    def preview(self, destination_id: str, mode: RemovalMode) -> RemovalPreview:
        """What the removal would do, in numbers; nothing is written, nothing is locked."""
        with self._engine.connect() as conn:
            self._context(conn, destination_id)
            records = _records(conn, destination_id)
        managed = len([r for r in records if _is_managed(r)])
        adopted = len([r for r in records if r.state == "adopted"])
        warnings: list[RemovalWarning] = []
        if mode is RemovalMode.KEEP_FILES:
            warnings.append(RemovalWarning("files_kept", managed + adopted))
        else:
            warnings.append(RemovalWarning("files_deleted", managed))
            warnings.append(RemovalWarning("past_the_bin", managed))  # deleted for good
            if adopted:
                warnings.append(RemovalWarning("adopted_kept", adopted))
        warnings.append(RemovalWarning("manifest_forgotten", len(records)))
        return RemovalPreview(destination_id, mode, tuple(warnings), removal_phrase(destination_id))

    # -- the removal ----------------------------------------------------------------------------

    def apply(
        self,
        destination_id: str,
        mode: RemovalMode,
        *,
        principal: Principal,
        typed: str | None,
        job_id: str | None = None,
    ) -> RemovalReport:
        self._authorize(principal)
        if typed is None or not hmac.compare_digest(
            typed.strip().encode(), removal_phrase(destination_id).encode()
        ):
            raise RemovalRefusedError("confirmation_required")
        with self._engine.connect() as conn:
            ctx = self._context(conn, destination_id)
        with self._held(ctx, job_id or new_ulid()):
            report = RemovalReport()
            left: list[LeftLine] = []
            self._settle_own(destination_id, ctx, report, left, resume=True)
            with self._engine.connect() as conn:
                self._require_free(conn, ctx, destination_id)
                records = _records(conn, destination_id)  # read once the destination is ours
            if mode is RemovalMode.DELETE_FILES:
                self._delete_files(ctx, records, report, left)
            report.left = tuple(left)
            if report.complete:
                with write_transaction(self._engine) as conn:
                    report.forgotten += forget_destination(
                        conn, destination_id, copy_scope(ctx, self._roots)
                    )
                    self._audit(conn, principal, destination_id, mode)
                if report.deleted and self._request_scan is not None and ctx.root_local:
                    self._request_scan(ctx.remote_folder(ctx.root_local))
        log.info(
            "destination.removed", destination_id=destination_id, mode=mode.value,
            deleted=report.deleted, left=len(report.left),
        )  # fmt: skip
        return report

    def settle(self, destination_id: str, *, resume: bool, principal: Principal) -> RemovalReport:
        """After a stop: finish what the interrupted removal had begun (`resume`), or give it up.
        Giving up cancels the intentions whose file is still there; a file already gone has its
        line closed either way, because the manifest follows the disk."""
        self._authorize(principal)
        with self._engine.connect() as conn:
            ctx = self._context(conn, destination_id)
        report = RemovalReport()
        left: list[LeftLine] = []
        with self._held(ctx, new_ulid()):
            self._settle_own(destination_id, ctx, report, left, resume=resume)
        report.left = tuple(left)
        return report

    # -- the steps ------------------------------------------------------------------------------

    def _delete_files(
        self,
        ctx: DestinationContext,
        records: Sequence[Any],
        report: RemovalReport,
        left: list[LeftLine],
    ) -> None:
        scope = copy_scope(ctx, self._roots)
        emptied: set[str] = set()
        for record in sorted(records, key=lambda r: str(r.rel_path)):
            if not _is_managed(record):
                continue
            rel = str(record.rel_path)
            local = self._inside(ctx, rel)
            if local is None:
                left.append(LeftLine("outside_root", rel))
                continue
            found = self._look(local)
            if found == "gone":  # the file is not there: only the line is left to forget
                with write_transaction(self._engine) as conn:
                    _forget_line(conn, ctx.destination_id, record.export_item_id, scope)
                continue
            if found != "file":
                left.append(LeftLine("not_a_regular_file", rel))
                continue
            if not record.file_hash:  # nothing says this file is Shelfhold's own: never deleted
                left.append(LeftLine("no_hash", rel))
                continue
            made = self._record(ctx, record, local)
            if made is None:
                left.append(LeftLine("refused_by_the_disk", rel))
                continue
            intent, identity = made
            outcome = self._remove(intent, local, record.file_hash, identity)
            if outcome == "deleted":
                self._close(ctx.destination_id, record.export_item_id, intent, scope)
                report.deleted += 1
                emptied.add(posixpath.dirname(local))
            else:
                left.append(LeftLine(outcome, rel))
        self._remove_emptied(ctx, emptied)

    def _record(
        self, ctx: DestinationContext, record: Any, local: str
    ) -> tuple[str, FileIdentity] | None:
        try:
            observed = self._fsops.observe(local)
        except FsopsError:
            return None
        if observed is None or observed[0] != "file":
            return None
        intent = self._journal.record(
            op="unlink", src=local, expected_sha256=record.file_hash, expected_file_id=observed[1],
            extra={
                "scope": REMOVAL_SCOPE, "destination_id": ctx.destination_id,
                "item_id": record.export_item_id,
            },
        )  # fmt: skip
        return intent, observed[1]

    def _remove(
        self,
        intent_id: str,
        local: str,
        sha256: str | None,
        identity: FileIdentity | None,
    ) -> str:
        """`applying`, then the file goes, only if it is still the one the intention names (its
        content and its identity are read again by the primitive, just before the unlink).
        `deleted`, or why it stayed: `changed_since_export`, `refused_by_the_disk` (the intention
        is then `cancelled`: nothing moved)."""
        self._journal.mark_applying(intent_id)
        try:
            self._fsops.purge_unlink(local, expected_sha256=sha256, expected_identity=identity)
        except FileChangedError:
            self._journal.cancel(intent_id)
            return "changed_since_export"
        except (OSError, FsopsError):
            self._journal.cancel(intent_id)
            return "refused_by_the_disk"
        return "deleted"

    def _close(
        self, destination_id: str, item_id: str, intent_id: str, scope: CopyScope | None
    ) -> None:
        with write_transaction(self._engine) as conn:
            self._journal.mark_done(intent_id, within=conn)
            _forget_line(conn, destination_id, item_id, scope)

    def _remove_emptied(self, ctx: DestinationContext, folders: set[str]) -> None:
        """A series folder the removal emptied goes, deepest first, each by a journaled `rmdir`;
        the destination's own folder and a folder with anything left in it never."""
        root = ctx.root_local
        if root is None:
            return
        base = root.rstrip("/")
        pending = {f for f in folders if f.startswith(base + "/")}
        while pending:
            local = max(pending, key=len)
            pending.discard(local)
            try:
                seen = self._fsops.observe(local)
                if seen is None:
                    pass
                elif seen[0] != "dir" or self._fsops.list_dir(local):
                    continue
                else:
                    self._fsops.require_delete_allowed(local)
                    intent = self._journal.record(op="rmdir", src=local)
                    self._journal.mark_applying(intent)
                    try:
                        self._fsops.rmdir(local)
                    except (FsopsError, OSError):
                        self._journal.cancel(intent)
                        continue
                    self._journal.mark_done(intent)
            except (FsopsError, OSError):
                continue
            parent = posixpath.dirname(local)
            if parent.startswith(base + "/"):
                pending.add(parent)

    def _settle_own(
        self,
        destination_id: str,
        ctx: DestinationContext,
        report: RemovalReport,
        left: list[LeftLine],
        *,
        resume: bool,
    ) -> None:
        scope = copy_scope(ctx, self._roots)
        for intent in self._journal.open_intents():
            if not _ours(intent, destination_id) or intent.src is None:
                continue
            item_id = str(intent.payload.get("item_id", ""))
            found = self._look(intent.src)
            if found == "gone":  # it went before the stop: close its line
                self._close(destination_id, item_id, intent.id, scope)
                report.deleted += 1
            elif resume and found == "file":  # the file is looked at again, as at the first pass
                outcome = self._remove(
                    intent.id, intent.src, intent.expected_sha256, intent.expected_file_id
                )
                if outcome == "deleted":
                    self._close(destination_id, item_id, intent.id, scope)
                    report.deleted += 1
                else:
                    left.append(LeftLine(outcome, self._relative(ctx, intent.src)))
            else:
                self._journal.cancel(intent.id)
                report.cancelled += 1

    @staticmethod
    def _relative(ctx: DestinationContext, local: str) -> str:
        rel = within(local, ctx.root_local) if ctx.root_local is not None else None
        return rel if rel else local.rsplit("/", 1)[-1]

    # -- the checks -----------------------------------------------------------------------------

    def _authorize(self, principal: Principal) -> None:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS[_ACTION], now=self._clock.now(),
            fresh_auth_s=self._fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):
            raise RemovalRefusedError("not_allowed", verdict=verdict)

    @staticmethod
    def _context(conn: Connection, destination_id: str) -> DestinationContext:
        exists = conn.execute(
            sa.select(export.shelf_destination.c.id).where(
                export.shelf_destination.c.id == destination_id
            )
        ).scalar()
        if exists is None:
            raise RemovalRefusedError("unknown_destination")
        ctx = load_context(conn, destination_id)
        if ctx.root_local is None:
            raise RemovalRefusedError("no_folder")
        return ctx

    @staticmethod
    def _inside(ctx: DestinationContext, rel: str) -> str | None:
        """The local path of a manifest line, or None when it is not under the destination's
        folder (a `..`, an absolute path, a forged line): never touched."""
        if not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
            return None
        try:
            local = clean_absolute(ctx.local(rel))
        except (PathMapError, ValueError):
            return None
        root = ctx.root_local
        return local if root is not None and within(local, root) not in (None, "") else None

    def _look(self, local: str) -> str:
        """`gone`, `file`, or `other` (a link, a folder, a host file, anything refused)."""
        try:
            found = self._fsops.observe(local)
        except FsopsError:
            return "other"
        if found is None:
            return "gone"
        return "file" if found[0] == "file" else "other"

    def _digest(self, local: str) -> str:
        return self._fsops.sha256_of(local)

    def _require_free(self, conn: Connection, ctx: DestinationContext, destination_id: str) -> None:
        """I9: an open file operation of somebody else over the destination's folder stops it."""
        if ctx.root_local is None:
            return
        mine = {i.id for i in self._journal.open_intents() if _ours(i, destination_id)}
        if mine:  # `_settle_own` left some of ours open (a disk that would not): stop here
            raise RemovalRefusedError("intent_open")
        bases = open_intent_paths(conn)
        if within_any(ctx.root_local, bases) or holds_any(ctx.root_local, bases):
            raise RemovalRefusedError("intent_open")

    @contextmanager
    def _held(self, ctx: DestinationContext, job_id: str) -> Iterator[None]:
        """The destination is held, alone, for the length of the removal (`dest:<id>`, exclusive:
        no export, scan or staging of it runs, and none starts), and released whatever happens;
        a lease that a kill left expires by itself."""
        if self._locks is None:
            yield
            return
        try:
            held = self._locks.hold_resources(job_id, [dest_resource(ctx.destination_id)])
            held.__enter__()
        except LockBusyError:
            raise RemovalRefusedError("busy") from None
        try:
            yield
        finally:
            held.__exit__(None, None, None)

    def _audit(
        self, conn: Connection, principal: Principal, destination_id: str, mode: RemovalMode
    ) -> None:
        conn.execute(
            audit_insert(
                AuditEntry(
                    actor_id=principal.actor_id,
                    action=_ACTION,
                    at=self._clock.now(),
                    target=f"{destination_id}:{mode.value}",
                    session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
        )


# -- the manifest ---------------------------------------------------------------------------------


def _is_managed(record: Any) -> bool:
    """A file Shelfhold wrote and still has there: `written`, with a path."""
    return bool(record.state == "written" and record.rel_path)


def _ours(intent: OpenIntent, destination_id: str) -> bool:
    return (
        intent.op == "unlink"
        and intent.payload.get("scope") == REMOVAL_SCOPE
        and intent.payload.get("destination_id") == destination_id
    )


def _records(conn: Connection, destination_id: str) -> list[Any]:
    record = export.export_record
    return list(
        conn.execute(sa.select(record).where(record.c.shelf_destination_id == destination_id))
    )


def _forget_line(
    conn: Connection, destination_id: str, item_id: str, scope: CopyScope | None
) -> None:
    record = export.export_record
    conn.execute(
        sa.delete(record).where(
            record.c.shelf_destination_id == destination_id, record.c.export_item_id == item_id
        )
    )
    _forget_copies(conn, item_id, scope)


def _forget_copies(conn: Connection, item_id: str, scope: CopyScope | None) -> None:
    """The `destination` copy rows of the item that lie in this destination's own folder, and
    only those: the same item may live in another destination of the shelf. Without a known
    root, no row is touched."""
    if scope is not None:
        forget_destination_copies(conn, item_id, physical_root_id=scope.root_id, under=scope.under)


def forget_destination(
    conn: Connection, destination_id: str, scope: CopyScope | None = None
) -> int:
    """The destination and everything that hangs on it in the database: the manifest, the series
    it kept for its folder and their links, the links to remote libraries. No file, no Kavita.
    Returns the number of manifest lines forgotten."""
    series = export.export_series
    series_ids = sa.select(series.c.id).where(series.c.shelf_destination_id == destination_id)
    record = export.export_record
    items = [
        r.export_item_id
        for r in conn.execute(
            sa.select(record.c.export_item_id).where(
                record.c.shelf_destination_id == destination_id
            )
        )
    ]
    conn.execute(sa.delete(export.edition_export_series).where(
        export.edition_export_series.c.export_series_id.in_(series_ids)
    ))  # fmt: skip
    conn.execute(sa.delete(export.field_owner).where(
        export.field_owner.c.shelf_destination_id == destination_id
    ))  # fmt: skip
    conn.execute(sa.delete(series).where(series.c.shelf_destination_id == destination_id))
    for item_id in items:
        _forget_copies(conn, item_id, scope)
    conn.execute(sa.delete(record).where(record.c.shelf_destination_id == destination_id))
    conn.execute(sa.delete(export.export_upgrade).where(
        export.export_upgrade.c.shelf_destination_id == destination_id
    ))  # fmt: skip
    conn.execute(sa.delete(export.shelf_destination_remote_library).where(
        export.shelf_destination_remote_library.c.shelf_destination_id == destination_id
    ))  # fmt: skip
    conn.execute(
        sa.delete(export.shelf_destination).where(export.shelf_destination.c.id == destination_id)
    )
    return len(items)
