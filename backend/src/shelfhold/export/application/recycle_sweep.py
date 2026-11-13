"""The retention of the recycle bins (ENF-OPS-009): an entry older than 30 days is removed.

Each bin is `<root>/.shelfhold/recycle/<id>`. The sweep asks `Fsops` whether an entry is old
enough, records the removal as an intention (`delete`), and removes it through the system
emitter `trash_retention_purge`, whose perimeter (the bin, nothing else) and age (read from the
file system, never from here) `Fsops` enforces again. A crash leaves the intention; the replay
(`IntentJournal`) never deletes, it closes it and the next night's sweep decides again.

An entry named after an open intent is kept whatever its age: a same-path upgrade (EF-EXP-016)
puts the old version in `recycle/<intent id>/`, and while that intent is open (a drift waiting
for an admin) the entry may hold the only copy of it. A removed entry leaves the bin index
(`trash_item`) in the transaction that closes its intent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
import structlog

from shelfhold.export.application.intents import (
    IntentJournal,
    open_intent_ids,
    open_intent_paths,
)
from shelfhold.export.domain.physical_roots import RECYCLE_DIR
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import EmitterConditionError, FsopsError
from shelfhold.infrastructure.schema import catalog, platform, storage
from shelfhold.library.public import (
    adopted_original_unprotected,
    bin_entry,
    forget_binned_export_files,
    safe_to_clean,
)

log = structlog.get_logger(__name__)


BackupsSince = Callable[[datetime], list[tuple[str, datetime]]]  # `ops.backup.report` shape


@dataclass(frozen=True)
class RecycleReport:
    removed: int
    kept: int
    failed: int = 0
    held_irreplaceable: int = (
        0  # entries kept, and flagged, because their Unit became irreplaceable
    )


class RecycleSweeper:
    def __init__(
        self,
        fsops: Fsops,
        journal: IntentJournal,
        roots: Sequence[str],
        *,
        backups: BackupsSince | None = None,
    ) -> None:
        self._fsops: Fsops = fsops
        self._journal = journal
        self._roots = tuple(roots)
        self._backups = backups

    def _retained_by_backup(self, name: str) -> bool:
        """The entry holds a copy kept in /store (a deletion of content put it in the bin) of a
        Unit that an integrated backup still retained would find in its database: the copy stays
        in the bin, past its 30 days, until no retained backup taken since the Unit existed is
        left (EF-BIB-021, GAP-2-17). Only `/store`: the other copies are Shelfhold's own, which
        a restored database does not wait for."""
        if self._backups is None:
            return False
        trash, unit, root = storage.trash_item, catalog.unit, platform.physical_root
        with self._journal.engine.connect() as connection:
            rows = connection.execute(
                sa.select(trash.c.snapshot, unit.c.created_at)
                .select_from(
                    trash.join(root, root.c.root_id == trash.c.physical_root_id).join(
                        unit, unit.c.id == trash.c.ref
                    )
                )
                .where(
                    trash.c.kind == "unit",
                    trash.c.snapshot.is_not(None),
                    sa.or_(
                        trash.c.path == name,
                        trash.c.path.startswith(f"{name}/", autoescape=True),
                    ),
                )
            ).all()
        for snapshot, created in rows:
            if (
                isinstance(snapshot, dict)
                and snapshot.get("place") == "store"
                and (self._backups(created))
            ):
                return True
        return False

    def _files(self, path: str) -> list[str]:
        try:
            listed = self._fsops.list_dir(path, host=True)
        except NotADirectoryError:
            return [path]
        found: list[str] = []
        for name, kind in sorted(listed.items()):
            if str(kind) == "file":
                found.append(f"{path}/{name}")
            elif str(kind) == "dir":
                found += self._files(f"{path}/{name}")
            # a link or a special file is never a copy to protect, and is never followed
        return found

    def _holds_only_copy(self, entry: str) -> bool:
        """The entry holds a file adopted in place (`adopted_ref`, no copy of Shelfhold's own) that
        has no independent, verified copy in /store: the bin may hold the only bytes there are
        (EF-BIB-021, EF-BIB-030). A file this does not recognise is not held."""
        for local in self._files(entry):
            try:
                digest = self._fsops.sha256_of(local, host=True)
            except (FsopsError, OSError):
                return True  # unreadable: it cannot be shown to be safe, so it is not swept
            with self._journal.engine.connect() as connection:
                if adopted_original_unprotected(connection, self._fsops, digest):
                    return True
        return False

    def _expired(self, name: str, entry: str) -> bool:
        """The term the deletion gave the entry (`trash_item.purge_after`), not the date of a
        folder on the disk. An entry the index does not know (a same-path upgrade's old version)
        keeps the age that `Fsops` reads from the file system."""
        with self._journal.engine.connect() as connection:
            term = bin_entry(connection, name).purge_after
        if term is None:
            return self._fsops.trash_expired(entry)
        return self._journal.clock.now() >= term.replace(tzinfo=self._journal.clock.now().tzinfo)

    def _became_irreplaceable(self, name: str) -> bool:
        """A Unit deleted while its source still listed it may have lost that source since: the
        bin then holds the only bytes there are. Read again, now, never from the day of the
        deletion (EF-BIB-031: kept and flagged, never erased in silence)."""
        with self._journal.engine.connect() as connection:
            named = bin_entry(connection, name).units
            units = sorted(  # a Unit purged since has no registry row left to ask
                connection.execute(
                    sa.select(catalog.unit.c.id).where(catalog.unit.c.id.in_(named))
                ).scalars()
            )
            _allowed, kept = safe_to_clean(connection, units) if units else ([], [])
        return bool(kept)

    def holds(self, name: str, entry: str) -> str | None:
        """Why this bin entry must stay whatever its age, or None (EF-BIB-021, EF-BIB-030,
        EF-BIB-031). Asked by the manual emptying and by the early purge, which only drop the
        age: an open intent (an upgrade's old version, a restore on its way, a removal begun), a
        Unit that became irreplaceable, a copy a retained backup may look for, an adopted
        original with no verified copy in /store. One of `intent`, `irreplaceable`, `backup`,
        `only_copy`."""
        with self._journal.engine.connect() as connection:
            paths = open_intent_paths(connection)
            if name in open_intent_ids(connection) or any(
                p in (name, entry) or p.startswith((f"{name}/", f"{entry}/")) for p in paths
            ):
                return "intent"
        if self._became_irreplaceable(name):
            return "irreplaceable"
        if self._retained_by_backup(name):
            return "backup"
        if self._holds_only_copy(entry):
            return "only_copy"
        return None

    def sweep(self) -> RecycleReport:
        removed = kept = failed = held = 0
        for root in self._roots:
            bin_path = f"{root}/{RECYCLE_DIR}"
            try:
                entries = sorted(self._fsops.list_dir(bin_path, host=True))
            except (FileNotFoundError, NotADirectoryError):
                continue  # no bin yet: nothing was ever recycled here
            for name in entries:
                entry = f"{bin_path}/{name}"
                with self._journal.engine.connect() as connection:
                    busy = name in open_intent_ids(connection) or any(
                        p == name or p.startswith(f"{name}/") for p in open_intent_paths(connection)
                    )
                if busy:
                    kept += 1  # an open upgrade's old version, or a restore on its way: never swept
                    log.info("retention.recycle_held_by_intent", entry=name)
                    continue
                if not self._expired(name, entry):
                    kept += 1  # younger than the retention: no intention for nothing
                    continue
                if self._became_irreplaceable(name):
                    kept += 1
                    held += 1
                    log.warning("retention.recycle_held_irreplaceable", entry=name)
                    continue
                if self._retained_by_backup(name):
                    kept += 1  # a retained backup may be restored and look for this copy
                    log.info("retention.recycle_held_by_backup", entry=name)
                    continue
                if self._holds_only_copy(entry):
                    kept += 1  # an adopted original with no verified copy in /store: never swept
                    log.warning("retention.recycle_held_adopted_original", entry=name)
                    continue
                intent = self._journal.record(op="delete", src=entry)
                self._journal.mark_applying(intent)
                try:
                    self._fsops.system_remove_tree("trash_retention_purge", entry)
                except EmitterConditionError:
                    self._journal.cancel(intent)  # it became younger in between: it stays
                    kept += 1
                except (FsopsError, OSError):
                    self._journal.cancel(intent)
                    failed += 1
                    log.warning("retention.recycle_failed")
                else:
                    with self._journal.engine.begin() as connection:
                        self._journal.mark_done(intent, within=connection)
                        forget_binned_export_files(connection, name)
                    removed += 1
        return RecycleReport(removed, kept, failed, held)
