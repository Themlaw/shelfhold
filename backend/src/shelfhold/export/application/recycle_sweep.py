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

from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from shelfhold.export.application.intents import IntentJournal, open_intent_ids
from shelfhold.export.domain.physical_roots import RECYCLE_DIR
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import EmitterConditionError, FsopsError
from shelfhold.library.public import adopted_original_unprotected, forget_binned_export_files

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RecycleReport:
    removed: int
    kept: int
    failed: int = 0


class RecycleSweeper:
    def __init__(self, fsops: Fsops, journal: IntentJournal, roots: Sequence[str]) -> None:
        self._fsops: Fsops = fsops
        self._journal = journal
        self._roots = tuple(roots)

    def _files(self, path: str) -> list[str]:
        try:
            listed = self._fsops.list_dir(path, host=True)
        except NotADirectoryError:
            return [path]
        found: list[str] = []
        for name, kind in sorted(listed.items()):
            found += self._files(f"{path}/{name}") if str(kind) != "file" else [f"{path}/{name}"]
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
                if adopted_original_unprotected(connection, digest):
                    return True
        return False

    def sweep(self) -> RecycleReport:
        removed = kept = failed = 0
        for root in self._roots:
            bin_path = f"{root}/{RECYCLE_DIR}"
            try:
                entries = sorted(self._fsops.list_dir(bin_path, host=True))
            except (FileNotFoundError, NotADirectoryError):
                continue  # no bin yet: nothing was ever recycled here
            for name in entries:
                entry = f"{bin_path}/{name}"
                with self._journal.engine.connect() as connection:
                    held = name in open_intent_ids(connection)
                if held:
                    kept += 1  # the old version of an open upgrade: never swept
                    log.info("retention.recycle_held_by_intent", entry=name)
                    continue
                if not self._fsops.trash_expired(entry):
                    kept += 1  # younger than the retention: no intention for nothing
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
        return RecycleReport(removed, kept, failed)
