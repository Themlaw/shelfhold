"""Restore epoch and `restored_behind`, as pure rules (EF-INT-026, I28, I36, I39).

A database that went back in time (restore of a backup, rollback of an image) finds the disk
ahead of it: the sentinels carry a higher generation, or a higher restore epoch. That is neither
"OK" nor "a different folder": the files that are newer than the database are not foreign, so
nothing may be written, deleted or scanned until `reconcile --adopt-after-restore` re-adopts them.

This module holds what needs no I/O: the status of the instance, the verdict on one line of the
provenance mirror, and the fingerprint that ties a confirmation to the preview the admin saw.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from shelfhold.export.domain.physical_roots import RootState

__all__ = [
    "EXIT_COMMAND",
    "LineFacts",
    "LineOutcome",
    "RestoreStatus",
    "RootFinding",
    "fingerprint_of",
    "judge_line",
    "safe_rel_path",
]

EXIT_COMMAND: Final = "shelfhold reconcile --adopt-after-restore"
MAX_REL_PATH: Final = 1024


@dataclass(frozen=True)
class RootFinding:
    """One read-write root, as the database and its sentinel see it."""

    root_id: str
    path: str
    state: RootState
    db_generation: int
    sentinel_generation: int | None
    sentinel_epoch: int | None


@dataclass(frozen=True)
class RestoreStatus:
    epoch: int  # `instance.restore_epoch` as it stands
    findings: tuple[RootFinding, ...]
    # The freeze is persisted (`instance.restore_generation` > 0): it lasts until the adoption,
    # whatever the sentinels say after a restart.
    pending: bool = False
    # `(sessions, api_keys)` revoked by this very call (the first start behind the disk): the
    # caller leaves the durable notice. None: nothing was revoked now.
    revoked: tuple[int, int] | None = None

    @property
    def behind_roots(self) -> tuple[RootFinding, ...]:
        return tuple(f for f in self.findings if f.state is RootState.RESTORED_BEHIND)

    @property
    def behind(self) -> bool:
        return self.pending or bool(self.behind_roots)

    @property
    def exit_command(self) -> str:
        return EXIT_COMMAND


def fingerprint_of(document: object) -> str:
    """sha256 of the canonical JSON of what the admin is shown; any change in it changes this."""
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def safe_rel_path(rel: object) -> str | None:
    """A relative path taken from the mirror (an untrusted file): clean, inside the destination,
    never reserved. None when it is anything else."""
    if not isinstance(rel, str) or not rel or len(rel) > MAX_REL_PATH or "\x00" in rel:
        return None
    parts = rel.split("/")
    if rel.startswith("/") or any(p in ("", ".", "..") for p in parts) or len(parts) < 2:
        return None
    if parts[0].startswith(".shelfhold"):
        return None
    return rel


class LineOutcome(StrEnum):
    READOPT = "readopt"  # re-adopted as exported, with its lineage
    KNOWN = "known"  # the manifest already has it
    ABSENT = "absent"  # a line without a file is ignored
    MODIFIED = "modified"  # the file is not the one the line describes: nothing is granted
    UNRESOLVED = "unresolved"  # the restored database cannot place it: kept, reported
    TOMBSTONED = "tombstoned"  # under a tombstone: never re-adopted; deletion needs the admin


@dataclass(frozen=True)
class LineFacts:
    on_disk: bool
    hash_matches: bool  # the disk file's hash is the line's `file_hash` (only asked when needed)
    known_in_manifest: bool
    tombstoned: bool
    lineage_granted: bool  # `grant_lineage` accepted the line
    unresolved_reason: str | None  # why the restored database cannot place it, if it cannot


def judge_line(facts: LineFacts) -> tuple[LineOutcome, str | None]:
    """The order is the safety argument: a tombstone is read before anything is adopted, and a
    file that is not the described one is never given a lineage."""
    if not facts.on_disk:
        return LineOutcome.ABSENT, None
    if facts.tombstoned:
        return LineOutcome.TOMBSTONED, None
    if facts.known_in_manifest:
        return LineOutcome.KNOWN, None
    if not facts.hash_matches:
        return LineOutcome.MODIFIED, None
    if not facts.lineage_granted:
        return LineOutcome.UNRESOLVED, "lineage"
    if facts.unresolved_reason is not None:
        return LineOutcome.UNRESOLVED, facts.unresolved_reason
    return LineOutcome.READOPT, None


def sorted_unique(items: Iterable[str]) -> list[str]:
    return sorted(set(items))
