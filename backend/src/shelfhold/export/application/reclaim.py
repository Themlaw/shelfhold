"""« Récupérer de la place » (EF-BIB-030): the trash emptied now, and the trash purged early when
the disk is under its mark. The bins are `<root>/.shelfhold/recycle/<entry>`; nothing else is ever
removed here.

- **Empty now** is a reserved action (`trash.empty`: a fresh session or the host CLI, never a key
  or a token) with a typed word compared on the server. It drops the 30 days, nothing else: an entry
  that an open intent names (an upgrade's old version, a restore on its way), a Unit that became
  irreplaceable, a copy that a retained backup may look for and an adopted original with no verified
  copy in /store all stay, and are counted. Removal goes through `Fsops.purge_remove_tree`, the
  primitive that only takes a bin entry (a directory, never through a link), under an intention.
- **Early purge** is the system emitter `disk_guard_purge` (a closed list, N2): it acts only while
  the disk guard says the volume is under its mark, only on an entry a week old at least (fsops
  reads the age and asks the guard itself), oldest first, a bounded number per run, and stops as
  soon as the pressure is over. It never removes what `holds` keeps (an irreplaceable Unit's entry
  first). Each run that did something leaves an audit line with a count and no name.

Two runs at once are serialised in the process (`_RUN_LOCK`); between processes the instance lock
(`flock` on `/data/.lock`) holds. An entry that another run took already is not an error.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
import structlog

from shelfhold.export.application.intents import BIN_LOCK, IntentJournal
from shelfhold.export.application.recycle_sweep import BackupsSince, RecycleSweeper
from shelfhold.export.domain.physical_roots import RECYCLE_DIR
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.emitters import EARLY_PURGE_MIN_AGE_S
from shelfhold.infrastructure.fsops.errors import EmitterConditionError, FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity
from shelfhold.library.public import (
    EARLY_PURGE_BATCH,
    EMPTY_TRASH_PHRASE,
    BinCandidate,
    TrashConfirmationError,
    bin_entry,
    forget_binned_export_files,
    oldest_first,
    require_empty_phrase,
)

__all__ = [
    "EARLY_PURGE_BATCH",
    "EARLY_PURGE_MIN_AGE_S",
    "EMPTY_TRASH_PHRASE",
    "BinReclaimer",
    "ReclaimReport",
    "TrashConfirmationError",
    "TrashEmptyRefusedError",
]

log = structlog.get_logger(__name__)
SYSTEM_ACTOR = "system"
_RUN_LOCK = threading.Lock()
_HOLD_KEY = {"intent": "kept", "irreplaceable": "irreplaceable"}  # any other hold: "other"


class TrashEmptyRefusedError(PermissionError):
    """The reserved-action rule refused the caller (a key, a token, a member, a stale session)."""

    def __init__(self, verdict: object) -> None:
        super().__init__(f"trash.empty refused: {type(verdict).__name__}")
        self.verdict = verdict


@dataclass(frozen=True)
class ReclaimReport:
    removed: int = 0
    kept: int = 0  # younger than the minimum, not a directory, gone meanwhile, or no pressure
    held_irreplaceable: int = 0  # kept because the Unit became irreplaceable (EF-BIB-031)
    held_other: int = 0  # kept by an open intent, a retained backup, an adopted original
    failed: int = 0


class BinReclaimer:
    def __init__(
        self,
        fsops: Fsops,
        journal: IntentJournal,
        roots: Sequence[str],
        *,
        clock: Clock,
        fresh_auth_s: int = 900,
        hardened: bool = False,
        backups: BackupsSince | None = None,
    ) -> None:
        self._fsops = fsops
        self._journal = journal
        self._roots = tuple(roots)
        self._clock = clock
        self._fresh_auth_s = fresh_auth_s
        self._hardened = hardened
        self._holds = RecycleSweeper(fsops, journal, roots, backups=backups)

    # -- the bins ----------------------------------------------------------------------------

    def _entries(self, root: str) -> dict[str, str]:
        """`name -> kind` of what lies directly in the bin of a root (none when it has no bin)."""
        try:
            return {
                n: str(k)
                for n, k in self._fsops.list_dir(f"{root}/{RECYCLE_DIR}", host=True).items()
            }
        except (FileNotFoundError, NotADirectoryError):
            return {}

    def _term(self, name: str) -> datetime | None:
        with self._journal.engine.connect() as connection:
            return bin_entry(connection, name).purge_after

    @staticmethod
    def _held(counts: dict[str, int], why: str | None) -> bool:
        """Count an entry the holds keep (an open intent counts as kept)."""
        if why is None:
            return False
        counts[_HOLD_KEY.get(why, "other")] += 1
        return True

    def _close(self, intent: str, name: str) -> None:
        with write_transaction(self._journal.engine) as connection:
            self._journal.mark_done(intent, within=connection)
            forget_binned_export_files(connection, name)  # the index follows the bin

    def _audit(self, action: str, removed: int, principal: Principal | None) -> None:
        with write_transaction(self._journal.engine) as connection:
            connection.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(), action=action,
                    actor_id=principal.actor_id if principal else SYSTEM_ACTOR,
                    target=f"entries:{removed}",
                    session_id=principal.session_id if principal else None,
                    auth_time=principal.auth_time if principal else None,
                )
            )  # fmt: skip

    # -- « Vider la corbeille » ---------------------------------------------------

    def empty_trash(self, *, principal: Principal, typed: str | None) -> ReclaimReport:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["trash.empty"], now=self._clock.now(),
            fresh_auth_s=self._fresh_auth_s, hardened=self._hardened,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):
            raise TrashEmptyRefusedError(verdict)
        require_empty_phrase(typed)
        counts = {"removed": 0, "kept": 0, "irreplaceable": 0, "other": 0, "failed": 0}
        with _RUN_LOCK:
            for root in self._roots:
                for name, kind in sorted(self._entries(root).items()):
                    entry = f"{root}/{RECYCLE_DIR}/{name}"
                    if kind != "dir":  # a link or a stray file is not an entry: left alone
                        counts["kept"] += 1
                        continue
                    with (
                        BIN_LOCK
                    ):  # a restore of this entry is not between the check and the removal
                        if self._held(counts, self._holds.holds(name, entry)):
                            continue
                        if self._intent_opened_since(counts, name, entry):
                            continue
                        self._empty_trash_entry(name, entry, counts)
        self._audit("trash.emptied", counts["removed"], principal)
        log.info("trash.emptied", removed=counts["removed"], kept=counts["kept"])
        return _report(counts)

    def _intent_opened_since(self, counts: dict[str, int], name: str, entry: str) -> bool:
        """The last look, right before the removal: an intent that was opened after the holds were
        asked (a restore that committed in between) keeps the entry."""
        if not self._holds.intent_open(name, entry):
            return False
        counts["kept"] += 1
        return True

    def _empty_trash_entry(self, name: str, entry: str, counts: dict[str, int]) -> None:
        intent = self._journal.record(op="delete", src=entry)
        self._journal.mark_applying(intent)
        try:
            self._fsops.purge_remove_tree(entry)
        except FileNotFoundError:
            self._journal.cancel(intent)  # another run took it first: nothing left to do
            counts["kept"] += 1
        except (FsopsError, OSError):
            self._journal.cancel(intent)
            counts["failed"] += 1
            log.warning("trash.empty_failed")
        else:
            self._close(intent, name)
            counts["removed"] += 1

    # -- the early purge under disk pressure (system emitter `disk_guard_purge`) -------------

    def early_purge(
        self,
        *,
        pressure: Callable[[], bool],
        after_each: Callable[[], None] | None = None,
        limit: int = EARLY_PURGE_BATCH,
    ) -> ReclaimReport:
        counts = {"removed": 0, "kept": 0, "irreplaceable": 0, "other": 0, "failed": 0}
        if not pressure():
            return _report(counts)
        with _RUN_LOCK:
            candidates = oldest_first(
                BinCandidate(root, name, self._term(name))
                for root in self._roots
                for name, kind in self._entries(root).items()
                if kind == "dir"
            )
            for candidate in candidates:
                if counts["removed"] >= limit or not pressure():
                    break
                entry = f"{candidate.root}/{RECYCLE_DIR}/{candidate.name}"
                if not self._fsops.early_purge_allowed(entry):
                    counts["kept"] += 1  # younger than the minimum, or its volume is fine
                    continue
                with BIN_LOCK:
                    if self._held(counts, self._holds.holds(candidate.name, entry)):
                        continue
                    if self._intent_opened_since(counts, candidate.name, entry):
                        continue
                    removed = self._remove_under_pressure(candidate.name, entry, counts)
                if removed and after_each:
                    after_each()
        if counts["removed"] or counts["irreplaceable"]:
            self._audit("trash.early_purged", counts["removed"], None)
            log.warning("trash.early_purged", removed=counts["removed"],
                        held_irreplaceable=counts["irreplaceable"])  # fmt: skip
        return _report(counts)

    def _remove_under_pressure(self, name: str, entry: str, counts: dict[str, int]) -> bool:
        intent = self._journal.record(op="delete", src=entry)
        self._journal.mark_applying(intent)
        try:
            self._fsops.system_remove_tree("disk_guard_purge", entry)
        except EmitterConditionError:
            self._journal.cancel(intent)  # younger, or the pressure ended in between: it stays
            counts["kept"] += 1
            return False
        except (FsopsError, OSError):
            self._journal.cancel(intent)
            counts["failed"] += 1
            return False
        self._close(intent, name)
        counts["removed"] += 1
        return True


def _report(counts: dict[str, int]) -> ReclaimReport:
    return ReclaimReport(
        removed=counts["removed"], kept=counts["kept"],
        held_irreplaceable=counts["irreplaceable"], held_other=counts["other"],
        failed=counts["failed"],
    )  # fmt: skip
