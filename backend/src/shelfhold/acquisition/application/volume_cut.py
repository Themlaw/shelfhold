"""The cut of a novel into volumes when its source declares none (EF-SRC-032, EF-BIB-040, D5).

A `ranged_content` source that names volumes (`volume_label`) is followed as it says. One that
does not, lncrawl for a novel it knows only as a list of chapters, is cut here: the main chapters,
in the order of the source, into volumes of the size of the Edition (`DEFAULT_VOLUME_SIZE` when
the Edition says none that makes sense), labelled `Vol.N`. Every full volume is closed (frozen:
hash stable, never regenerated); the short last one is closed only when the novel is finished.

The policy of the Edition then decides which of them are asked now (`decide_export`): under
`closed_only` the open tail waits, `rolling_tail` asks it too (and it is replaced at the same path
as chapters join it), `whole` is one file for a finished novel and nothing before. A policy that
nobody knows cuts nothing, never silently the default; an Edition that holds adopted volumes is
not cut either: its volumes were numbered by someone else.

Pure: the listing and the facts come from the caller, nothing is read or written here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from shelfhold.library.public import (
    DEFAULT_POLICY,
    DEFAULT_VOLUME_SIZE,
    VolumePolicy,
    decide_export,
    parse_policy,
    volume_slices,
)
from shelfhold.sdk.refs import UnitInfo

__all__ = ["CUT_MARK", "VolumeCut", "cut_volumes", "is_cut"]

CUT_MARK = "volume_cut"  # in `UnitInfo.memo`: the volume is ours, the source named none


@dataclass(frozen=True)
class VolumeCut:
    infos: list[UnitInfo]  # the listing, with the volume of each chapter that was cut
    waiting: frozenset[str]  # remote ids of cut chapters the policy does not ask for now
    cut: frozenset[str]  # remote ids that got a volume of ours (never one the source named)


def cut_volumes(
    infos: Sequence[UnitInfo],
    *,
    policy: str | None,
    size: int | None,
    finished: bool,
    adopted: bool,
    cap: int | None = None,
) -> VolumeCut:
    """`infos` with a volume on each main chapter when no chapter of the source has one. A volume
    is asked in one cycle, so it holds at most `cap` chapters (the politeness cap of a key): the
    size of the Edition is bounded by it."""
    unchanged = VolumeCut(list(infos), frozenset(), frozenset())
    if adopted or any(i.volume_label for i in infos):
        return unchanged
    try:
        chosen = DEFAULT_POLICY if policy is None else parse_policy(policy)
    except ValueError:
        return unchanged
    chapters = sorted((i for i in infos if i.track == "main"), key=lambda i: i.source_order)
    if not chapters:
        return unchanged
    if chosen is VolumePolicy.WHOLE:
        per = len(chapters)
    else:
        per = size if size is not None and size >= 1 else DEFAULT_VOLUME_SIZE
    if cap is not None and cap >= 1:
        per = min(per, cap)
    slices = volume_slices(len(chapters), per, finished=finished)
    labelled: dict[str, UnitInfo] = {}
    waiting: set[str] = set()
    for volume in slices:
        closed = finished if chosen is VolumePolicy.WHOLE else volume.closed
        asked = decide_export(chosen, volume_closed=closed, novel_finished=finished).exports
        for chapter in chapters[volume.start : volume.stop]:
            labelled[chapter.remote_unit_id] = chapter.model_copy(
                update={
                    "volume_label": f"Vol.{volume.index + 1}",
                    "volume_closed": closed,
                    "memo": {**chapter.memo, CUT_MARK: True},
                }
            )
            if not asked:
                waiting.add(chapter.remote_unit_id)
    return VolumeCut(
        [labelled.get(i.remote_unit_id, i) for i in infos],
        frozenset(waiting),
        frozenset(labelled),
    )


def is_cut(info: UnitInfo) -> bool:
    """The volume of this chapter was made by the cut, not named by its source."""
    return info.memo.get(CUT_MARK) is True
