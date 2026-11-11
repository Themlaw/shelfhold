"""How a web novel in progress is packaged into volumes (EF-BIB-040, EF-SRC-032, D5).

Three policies, per Edition (`edition.volume_policy`):

- `closed_only` (the default): only a volume the source announced closed is exported, and a closed
  volume is frozen: the file and its hash never change when new chapters arrive (they belong to
  the next volume);
- `rolling_tail`: the volume in progress is exported too, and replaced at the same path (L16) as
  chapters join it, with a warning each time; it never rewrites what a reader finished, and a
  smaller or stubbed replacement goes through the suspect-revision guard (L17) like any other;
- `whole`: one file for the whole novel, for a finished novel only.

Everything here is a pure decision from facts the caller reads; nothing touches a disk or a
database, so the rules can be attacked one by one.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

__all__ = [
    "DEFAULT_POLICY",
    "DEFAULT_VOLUME_SIZE",
    "ExportDecision",
    "ExportVerdict",
    "ReplaceDecision",
    "ReplaceVerdict",
    "VolumePolicy",
    "VolumeSlice",
    "decide_export",
    "decide_replacement",
    "parse_policy",
    "volume_slices",
]

DEFAULT_VOLUME_SIZE: Final = 50  # D5: chapters per volume when the source declares no volumes
TAIL_WARNING: Final = "volume_tail_replaced"


class VolumePolicy(enum.StrEnum):
    CLOSED_ONLY = "closed_only"
    ROLLING_TAIL = "rolling_tail"
    WHOLE = "whole"


DEFAULT_POLICY: Final = VolumePolicy.CLOSED_ONLY


def parse_policy(raw: str) -> VolumePolicy:
    """The policy named `raw`; anything else is refused (never silently the default)."""
    try:
        return VolumePolicy(raw)
    except ValueError:
        raise ValueError(f"not a volume policy: {raw!r}") from None


class ExportVerdict(enum.Enum):
    EXPORT = "export"  # a closed volume (or the whole of a finished novel): written once
    EXPORT_TAIL = "export_tail"  # the volume in progress, with a warning
    WAIT = "wait"  # not now: the volume is not closed (yet)
    REFUSE = "refuse"  # never, for this novel under this policy


@dataclass(frozen=True)
class ExportDecision:
    verdict: ExportVerdict
    reason: str | None = None
    warning: str | None = None

    @property
    def exports(self) -> bool:
        return self.verdict in (ExportVerdict.EXPORT, ExportVerdict.EXPORT_TAIL)


def decide_export(
    policy: VolumePolicy, *, volume_closed: bool, novel_finished: bool
) -> ExportDecision:
    """May this volume leave Shelfhold now? A volume that is not closed leaves only under
    `rolling_tail`; `whole` needs a finished novel and a closed file."""
    if policy is VolumePolicy.WHOLE:
        if not novel_finished:
            return ExportDecision(ExportVerdict.REFUSE, "novel_not_finished")
        if not volume_closed:
            return ExportDecision(ExportVerdict.WAIT, "volume_open")
        return ExportDecision(ExportVerdict.EXPORT)
    if volume_closed:
        return ExportDecision(ExportVerdict.EXPORT)
    if policy is VolumePolicy.ROLLING_TAIL:
        return ExportDecision(ExportVerdict.EXPORT_TAIL, warning=TAIL_WARNING)
    return ExportDecision(ExportVerdict.WAIT, "volume_open")


class ReplaceVerdict(enum.Enum):
    FIRST_WRITE = "first_write"  # nothing served yet: a plain placement
    REPLACE = "replace"  # same path, through L16 (the old file goes to the bin)
    KEEP_FROZEN = "keep_frozen"  # a closed volume: never regenerated
    KEEP_POLICY = "keep_policy"  # served while open, but the policy does not roll the tail
    KEEP_READ = "keep_read"  # a reader finished it and the shelf keeps read files
    REFUSE_SUSPECT = "refuse_suspect"  # the replacement is smaller or stubbed (L17)


@dataclass(frozen=True)
class ReplaceDecision:
    verdict: ReplaceVerdict
    warning: str | None = None


def decide_replacement(
    policy: VolumePolicy,
    *,
    volume_closed: bool,
    served: bool,
    read: bool,
    skip_read: bool,
    suspect: bool,
) -> ReplaceDecision:
    """What to do with a new version of the file of a volume. The only door to a replacement is
    `REPLACE`, which the caller must carry out through the same-path upgrade (L16)."""
    if not served:
        return ReplaceDecision(ReplaceVerdict.FIRST_WRITE)
    if volume_closed:
        return ReplaceDecision(ReplaceVerdict.KEEP_FROZEN)
    if policy is not VolumePolicy.ROLLING_TAIL:
        return ReplaceDecision(ReplaceVerdict.KEEP_POLICY)
    if read and skip_read:
        return ReplaceDecision(ReplaceVerdict.KEEP_READ)
    if suspect:
        return ReplaceDecision(ReplaceVerdict.REFUSE_SUSPECT)
    return ReplaceDecision(ReplaceVerdict.REPLACE, TAIL_WARNING)


@dataclass(frozen=True)
class VolumeSlice:
    """Chapters `start` to `stop` (positions in the source order, `stop` excluded)."""

    index: int
    start: int
    stop: int
    closed: bool


def volume_slices(
    chapters: int, size: int = DEFAULT_VOLUME_SIZE, *, finished: bool = False
) -> list[VolumeSlice]:
    """Cut `chapters` into volumes of `size` (D5), for a source that declares none. Every full
    volume is closed; the last, short one is closed only when the novel is finished."""
    if size < 1:
        raise ValueError("a volume holds at least one chapter")
    if chapters < 0:
        raise ValueError("a novel has no negative number of chapters")
    slices = []
    for index, start in enumerate(range(0, chapters, size)):
        stop = min(start + size, chapters)
        slices.append(VolumeSlice(index, start, stop, closed=stop - start == size or finished))
    return slices
