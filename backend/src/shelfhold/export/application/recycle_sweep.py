"""The retention of the recycle bins (ENF-OPS-009): an entry older than 30 days is removed.

Each bin is `<root>/.shelfhold/recycle/<id>`. The sweep asks `Fsops` whether an entry is old
enough, records the removal as an intention (`delete`), and removes it through the system
emitter `trash_retention_purge`, whose perimeter (the bin, nothing else) and age (read from the
file system, never from here) `Fsops` enforces again. A crash leaves the intention; the replay
(`IntentJournal`) never deletes, it closes it and the next night's sweep decides again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.domain.physical_roots import RECYCLE_DIR
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import EmitterConditionError, FsopsError

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
                if not self._fsops.trash_expired(entry):
                    kept += 1  # younger than the retention: no intention for nothing
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
                    self._journal.mark_done(intent)
                    removed += 1
        return RecycleReport(removed, kept, failed)
