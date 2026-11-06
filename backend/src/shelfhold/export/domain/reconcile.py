"""The reduced reconcile of a tree destination, as pure decisions (EF-EXP-018, EF-EXP-043).

Nothing here touches the disk or the database: the application layer observes, these functions
say what the observation means. Shelfhold never deletes nor overwrites what a third party did;
it reports it, and pauses the series it cannot trust.
"""

from collections.abc import Mapping
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
    "Thresholds",
    "classify_change",
    "classify_missing",
    "missing_blocks",
    "thresholds_from",
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


@dataclass(frozen=True)
class Thresholds:
    """The three settable numbers of the rule (`export.missing_floor|ratio|ceiling`)."""

    floor: int = FLOOR_MISSING
    ratio: float = RATIO_MISSING
    ceiling: int = CEILING_MISSING


def thresholds_from(settings: Mapping[str, str]) -> Thresholds:
    """The stored settings over the defaults; a value that does not parse is ignored (the keys
    are validated when set, a hand-edited row must not stop the reconcile)."""

    def number(key: str, default: float, kind: type[int] | type[float]) -> float:
        try:
            value = kind(settings[key])
        except (KeyError, ValueError):
            return default
        return value if value > 0 else default

    return Thresholds(
        floor=int(number("export.missing_floor", FLOOR_MISSING, int)),
        ratio=number("export.missing_ratio", RATIO_MISSING, float),
        ceiling=int(number("export.missing_ceiling", CEILING_MISSING, int)),
    )


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
    dev: int | None = None  # of the replica row: what Shelfhold saw when it wrote the file
    ino: int | None = None


@dataclass(frozen=True)
class Seen:
    size: int
    mtime_ns: int
    regular: bool = True
    dev: int | None = None
    ino: int | None = None


class Change(StrEnum):
    UNCHANGED = "unchanged"
    SIZE_OR_KIND = "size_or_kind"  # certain: the hash cannot match
    MTIME_ONLY = "mtime_only"  # the hash decides (also: same size and mtime, another inode)
    NOT_RECORDED = "not_recorded"  # no identity in the manifest: nothing to compare against


def classify_change(managed: ManagedFile, seen: Seen) -> Change:
    """Size, then mtime (EF-EXP-043, GAP-4-07); the inode is compared by the replica registry."""
    if not seen.regular:
        return Change.SIZE_OR_KIND
    if managed.size is None or managed.mtime_ns is None:
        return Change.NOT_RECORDED
    if seen.size != managed.size:
        return Change.SIZE_OR_KIND
    if seen.mtime_ns != managed.mtime_ns:
        return Change.MTIME_ONLY
    # An atomic replacement (write a temporary file, rename it over) can keep size and mtime:
    # only the inode (and the device) tell, and the hash then decides.
    if (managed.ino is not None and seen.ino is not None and managed.ino != seen.ino) or (
        managed.dev is not None and seen.dev is not None and managed.dev != seen.dev
    ):
        return Change.MTIME_ONLY
    return Change.UNCHANGED


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
    if managed.reexport_attempted:  # put back once, gone again: whatever the delay, a habit
        return Disappeared.FOREIGN
    if not (recent and source_available and sentinel_ok):
        return Disappeared.ASK_ADMIN
    return Disappeared.REEXPORT
