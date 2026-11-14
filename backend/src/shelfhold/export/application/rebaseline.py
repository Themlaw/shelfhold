"""`reconcile --rebaseline-inodes`: the new baseline of a library that was moved to other disks
(EF-EXP-043, passe 8, GAP-8-07).

After a copy of the library (`rsync` keeping the mtimes) every file has another `(dev, ino)`. The
reconciliation reads that as files replaced by a third party and pauses the series. This puts the
new identities in the registry **without any pause**, but only for files it can prove are the
same:

- the size, the mtime and the `file_hash` of the file on disk are the ones the manifest records;
- the provenance mirror of the root (EF-EXP-063), which travelled with the files, has a line for
  the same path with the same `file_hash`. The mirror is read, never trusted alone: it confirms
  what the database and the disk already agree on.

A file whose bytes differ from the manifest is a modified file, not a moved one: **the whole
command is refused** and nothing is changed, so that a rebaseline can never hide a third party's
edit. A file that is gone, or whose mtime differs, or that the mirror does not confirm, is left as
it is (reported; the ordinary reconciliation judges it). A destination without a readable mirror
is refused. Afterwards `replica.independent` is recomputed in the only safe direction: a copy that
now shares its inode with another file of its Unit stops being counted as a copy of its own.
Nothing is written to a disk and nothing is deleted; the command takes the resource locks of the
folders it reads and answers `RebaselineRefusedError` if an export holds one.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application.locks import LockBusyError, ResourceLocks
from shelfhold.export.application.reconcile import Reconciler
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.path_map import clean_absolute, within
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.export.domain.provenance_mirror import MirrorDocument, MirrorFormatError
from shelfhold.export.domain.reconcile import LIVE_STATES
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform
from shelfhold.library.public import demote_shared_inodes, refresh_destination_identity

__all__ = ["RebaselineRefusedError", "RebaselineReport", "Rebaseliner"]


class RebaselineRefusedError(Exception):
    """Nothing was changed. `reason`: `hash_differs`, `mirror_unconfirmed`, `busy`, `blocked`."""

    code = "rebaseline.refused"

    def __init__(self, reason: str, files: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.files = files


@dataclass(frozen=True)
class RebaselineReport:
    updated: tuple[str, ...] = ()  # rel paths whose `(dev, ino)` are the new baseline
    unchanged: int = 0  # already right
    skipped: dict[str, str] = field(default_factory=dict)  # rel path -> why it was left alone
    demoted: int = 0  # copies that now share an inode and stopped counting as independent

    def as_dict(self) -> dict[str, object]:
        return {
            "updated": list(self.updated), "unchanged": self.unchanged,
            "skipped": dict(self.skipped), "demoted": self.demoted,
        }  # fmt: skip


@dataclass
class _Found:
    item_id: str
    unit_id: str
    rel_path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int


@dataclass
class _Plan:
    updates: list[_Found] = field(default_factory=list)
    differs: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    unchanged: int = 0


class Rebaseliner:
    def __init__(
        self, engine: Engine, fsops: Fsops, *, clock: Clock, locks: ResourceLocks | None = None
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._clock = clock
        self._locks = locks or ResourceLocks(engine, clock=clock)

    def run(self, destination_id: str | None = None) -> RebaselineReport:
        """Every `tree` destination (or the one named); the first refusal stops the command
        before the first change."""
        with self._engine.connect() as connection:
            ids = [destination_id] if destination_id else list(
                connection.execute(
                    sa.select(export.shelf_destination.c.id).order_by(export.shelf_destination.c.id)
                ).scalars()
            )  # fmt: skip
            contexts = [load_context(connection, d) for d in ids]
        usable = [c for c in contexts if c.transport == "tree" and c.root_local is not None]
        with ExitStack() as held:  # the locks come first: nothing is read that an export writes
            for context in usable:
                resources = [f"work:rebaseline-{context.destination_id}", *self._paths(context)]
                try:
                    held.enter_context(self._locks.hold_resources(new_ulid(), resources))
                except LockBusyError as busy:
                    raise RebaselineRefusedError("busy", (busy.resource,)) from None
            plans = [(c, self._plan(c)) for c in usable]
            wrong = sorted({p for _c, plan in plans for p in plan.differs})
            if wrong:
                raise RebaselineRefusedError("hash_differs", tuple(wrong))
            updated: list[str] = []
            unchanged = 0
            skipped: dict[str, str] = {}
            demoted = 0
            for context, plan in plans:
                demoted += self._apply(context, plan)
                updated += [f"{context.destination_id}/{f.rel_path}" for f in plan.updates]
                unchanged += plan.unchanged
                skipped.update(
                    {f"{context.destination_id}/{k}": v for k, v in plan.skipped.items()}
                )
        return RebaselineReport(tuple(updated), unchanged, skipped, demoted)

    def _paths(self, context: DestinationContext) -> list[str]:
        """`path:` resources of every series folder the destination has records in."""
        with self._engine.connect() as connection:
            managed = Reconciler._managed(connection, context.destination_id)
        folders = sorted({m.rel_path.split("/", 1)[0] for m in managed})
        return [f"path:{context.destination_id}/{folder}" for folder in folders]

    # -- planning: reads only --------------------------------------------------------------------

    def _plan(self, context: DestinationContext) -> _Plan:
        root = clean_absolute(context.root_local or "")
        with self._engine.connect() as connection:
            physical = self._physical_root(connection, root)
            managed = Reconciler._managed(connection, context.destination_id)
        if physical is None or self._fsops.status(physical).state is not RootState.OK:
            raise RebaselineRefusedError("blocked", (physical or root,))
        mirror = self._mirror_hashes(context, physical)
        plan = _Plan()
        for item in managed:
            if item.state not in LIVE_STATES:
                continue
            if item.file_hash is None:
                plan.skipped[item.rel_path] = "no_hash"  # nothing to confirm the bytes against
                continue
            local = f"{root}/{item.rel_path}"
            try:
                read = self._fsops.hash_and_identity(local)
            except FsopsError:
                plan.skipped[item.rel_path] = "unreadable"
                continue
            if read is None:
                plan.skipped[item.rel_path] = (
                    "missing" if self._fsops.observe(local) is None else "not_a_file"
                )
                continue
            digest, identity, dev = read  # one descriptor: these describe the same bytes
            if digest != item.file_hash or (item.size is not None and identity.size != item.size):
                plan.differs.append(f"{context.destination_id}/{item.rel_path}")
                continue
            if mirror.get(item.rel_path) != digest:
                plan.skipped[item.rel_path] = "mirror_does_not_confirm"
                continue
            if item.mtime_ns is not None and identity.mtime_ns != item.mtime_ns:
                plan.skipped[item.rel_path] = "mtime_differs"
                continue
            if (dev, identity.ino) == (item.dev, item.ino):
                plan.unchanged += 1
                continue
            plan.updates.append(
                _Found(
                    item.item_id,
                    item.unit_id,
                    item.rel_path,
                    dev,
                    identity.ino,
                    identity.size,
                    identity.mtime_ns,
                )
            )
        return plan

    @staticmethod
    def _physical_root(connection: sa.Connection, folder: str) -> str | None:
        """The writable physical root that carries `folder` (the mirror lives there)."""
        best: str | None = None
        table = platform.physical_root
        for row in connection.execute(sa.select(table.c.path_entry, table.c.mode)):
            path = clean_absolute(row.path_entry)
            if (
                row.mode == "rw"
                and within(folder, path) is not None
                and (best is None or len(path) > len(best))
            ):
                best = path
        return best

    def _mirror_hashes(self, context: DestinationContext, root: str) -> dict[str, str]:
        """`rel_path -> file_hash` of the mirror lines of this destination, from the root that
        carries its tree. Refused when the mirror cannot be read at all."""
        prefix = f"{context.destination_id}/"
        found: dict[str, str] = {}
        readable = False
        try:
            names = [n for n in self._fsops.list_mirror(root) if n.startswith(prefix)]
        except FsopsError:
            names = []
        for name in names:
            try:
                raw = self._fsops.read_mirror(root, name)
                document = MirrorDocument.parse(raw) if raw is not None else None
            except (MirrorFormatError, ValueError, FsopsError, RecursionError):
                continue  # a damaged mirror confirms nothing
            if document is None or document.read_only:
                continue
            readable = True
            for entry in document.entries:
                rel, digest = entry.get("rel_path"), entry.get("file_hash")
                if isinstance(rel, str) and isinstance(digest, str):
                    found[rel] = digest
        if not readable:
            raise RebaselineRefusedError("mirror_unconfirmed", (context.destination_id,))
        return found

    # -- applying: the registry only ---------------------------------------------------------------

    def _apply(self, context: DestinationContext, plan: _Plan) -> int:
        if not plan.updates:
            return 0
        now = self._clock.now()
        with write_transaction(self._engine) as connection:
            for found in plan.updates:
                refresh_destination_identity(
                    connection, found.item_id, dev=found.dev, ino=found.ino, size=found.size,
                    mtime_ns=found.mtime_ns, verified_at=now,
                )  # fmt: skip
            return demote_shared_inodes(connection, {f.unit_id for f in plan.updates})
