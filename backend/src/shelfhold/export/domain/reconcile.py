"""The reduced reconcile of a tree destination, as pure decisions (EF-EXP-018, EF-EXP-043).

Nothing here touches the disk or the database: the application layer observes, these functions
say what the observation means. Shelfhold never deletes nor overwrites what a third party did;
it reports it, and pauses the series it cannot trust.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

__all__ = [
    "FLOOR_MISSING",
    "REEXPORT_WINDOW",
    "Change",
    "Disappeared",
    "ManagedFile",
    "Seen",
    "classify_change",
    "classify_missing",
    "missing_blocks",
]

# EF-EXP-029 (GAP-5-14): per shelf destination, at least 10 missing files AND more than 5 %, or
# more than 50 missing files whatever the share. Two hand deletions in 20 files block nothing.
FLOOR_MISSING: Final = 10
RATIO_MISSING: Final = 0.05
CEILING_MISSING: Final = 50
REEXPORT_WINDOW: Final = timedelta(hours=24)  # EF-EXP-018
# `error_code` of a record that the reconcile already put back once: gone a second time, it is
# somebody else's habit (a nightly script), not an accident.
REEXPORT_ATTEMPTED: Final = "reconcile.reexport_attempted"
LIVE_STATES: Final = frozenset({"written", "scanned", "resolved", "adopted"})


def missing_blocks(
    *,
    total: int,
    missing: int,
    floor: int = FLOOR_MISSING,
    ratio: float = RATIO_MISSING,
    ceiling: int = CEILING_MISSING,
) -> bool:
    """True when the missing files look like an absent mount rather than a few deletions."""
    if missing > ceiling:
        return True
    return total > 0 and missing >= floor and missing / total > ratio


@dataclass(frozen=True)
class ManagedFile:
    """What the manifest says about a file in a managed series folder."""

    item_id: str
    unit_id: str
    rel_path: str
    state: str
    size: int | None
    mtime_ns: int | None
    file_hash: str | None
    written_at: datetime | None
    reexport_attempted: bool = False


@dataclass(frozen=True)
class Seen:
    size: int
    mtime_ns: int
    regular: bool = True


class Change(StrEnum):
    UNCHANGED = "unchanged"
    SIZE_OR_KIND = "size_or_kind"  # certain: the hash cannot match
    MTIME_ONLY = "mtime_only"  # the hash decides
    NOT_RECORDED = "not_recorded"  # no identity in the manifest: nothing to compare against


def classify_change(managed: ManagedFile, seen: Seen) -> Change:
    """Size, then mtime (EF-EXP-043, GAP-4-07); the inode is compared by the replica registry."""
    if not seen.regular:
        return Change.SIZE_OR_KIND
    if managed.size is None or managed.mtime_ns is None:
        return Change.NOT_RECORDED
    if seen.size != managed.size:
        return Change.SIZE_OR_KIND
    return Change.UNCHANGED if seen.mtime_ns == managed.mtime_ns else Change.MTIME_ONLY


class Disappeared(StrEnum):
    REEXPORT = "reexport"  # recently written by Shelfhold, a copy to put back
    FOREIGN = "foreign"  # already put back once and gone again
    ASK_ADMIN = "ask_admin"  # `missing`: re-export or stop following
    REPORT_ONLY = "report_only"  # an adopted file is never written by us


def classify_missing(
    managed: ManagedFile, *, now: datetime, source_available: bool, sentinel_ok: bool
) -> Disappeared:
    if managed.state == "adopted":
        return Disappeared.REPORT_ONLY
    recent = managed.written_at is not None and now - managed.written_at < REEXPORT_WINDOW
    if not (recent and source_available and sentinel_ok):
        return Disappeared.ASK_ADMIN
    return Disappeared.FOREIGN if managed.reexport_attempted else Disappeared.REEXPORT
