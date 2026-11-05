"""Waiting for Kavita to index what Shelfhold wrote (EF-EXP-034, M1a: polling only).

The first scan is asked when the file is written. At half the delay without arrival, a second
scan is sent (a watcher can miss an event). At the delay the file is declared not indexed, with
the probable causes, and the record is left to the admin: never a reexport, never "in progress"
without end (GAP-4-10).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any, Final

DEFAULT_DELAY: Final = timedelta(minutes=30)
SECOND_SCAN_AT: Final = 0.5  # of the delay


class Step(StrEnum):
    INDEXED = "indexed"
    WAIT = "wait"
    RESCAN = "rescan"
    NOT_INDEXED = "not_indexed"


def next_step(
    *,
    written_at: datetime,
    now: datetime,
    indexed: bool,
    rescanned: bool,
    delay: timedelta = DEFAULT_DELAY,
    max_scans: int = 2,
) -> Step:
    if indexed:
        return Step.INDEXED
    elapsed = now - written_at
    if elapsed >= delay:
        return Step.NOT_INDEXED
    if max_scans > 1 and not rescanned and elapsed >= delay * SECOND_SCAN_AT:
        return Step.RESCAN
    return Step.WAIT


def probable_causes(
    library: dict[str, Any] | None, remote_path: str, expected_type: int | None
) -> tuple[str, ...]:
    """What most often keeps Kavita from indexing a file: the three the admin can fix."""
    if library is None:
        return ("library_missing",)
    causes: list[str] = []
    patterns: Sequence[str] = library.get("excludePatterns") or ()
    if any(_excluded(p, remote_path) for p in patterns):
        causes.append("excluded_by_pattern")
    if expected_type is not None and library.get("type") != expected_type:
        causes.append("wrong_library_type")
    if not library.get("folderWatching", False):
        causes.append("folder_watching_off")
    return tuple(causes)


def _excluded(pattern: str, path: str) -> bool:
    """Kavita's globs (`**/.dir/**`, `*.tmp`) against a path, close enough for a hint."""
    path = path.lstrip("/")
    if fnmatchcase(path, pattern.lstrip("/")) or fnmatchcase(path.rsplit("/", 1)[-1], pattern):
        return True
    if pattern.startswith("**/") and pattern.endswith("/**"):
        folder = pattern[3:-3]
        return f"/{folder}/" in f"/{path}"
    return False
