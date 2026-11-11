"""M2 L26 (EF-BIB-040): the pure rules of the volume policy, each one attacked."""

import pytest

from shelfhold.library.public import (
    DEFAULT_POLICY,
    DEFAULT_VOLUME_SIZE,
    ExportVerdict,
    ReplaceVerdict,
    VolumePolicy,
    decide_export,
    decide_replacement,
    parse_policy,
    volume_slices,
)


@pytest.mark.req("EF-BIB-040")
def test_closed_only_is_the_default_and_never_exports_an_open_volume() -> None:
    assert DEFAULT_POLICY is VolumePolicy.CLOSED_ONLY
    open_volume = decide_export(VolumePolicy.CLOSED_ONLY, volume_closed=False, novel_finished=False)
    assert open_volume.verdict is ExportVerdict.WAIT and not open_volume.exports
    # a finished novel does not make an open volume exportable under closed_only
    finished = decide_export(VolumePolicy.CLOSED_ONLY, volume_closed=False, novel_finished=True)
    assert finished.verdict is ExportVerdict.WAIT
    closed = decide_export(VolumePolicy.CLOSED_ONLY, volume_closed=True, novel_finished=False)
    assert closed.verdict is ExportVerdict.EXPORT and closed.warning is None


@pytest.mark.req("EF-BIB-040")
def test_rolling_tail_exports_the_open_volume_with_a_warning() -> None:
    tail = decide_export(VolumePolicy.ROLLING_TAIL, volume_closed=False, novel_finished=False)
    assert tail.verdict is ExportVerdict.EXPORT_TAIL and tail.warning == "volume_tail_replaced"


@pytest.mark.req("EF-BIB-040")
def test_whole_policy_only_for_finished_novels_rules() -> None:
    for closed in (True, False):
        refused = decide_export(VolumePolicy.WHOLE, volume_closed=closed, novel_finished=False)
        assert refused.verdict is ExportVerdict.REFUSE and refused.reason == "novel_not_finished"
    assert (
        decide_export(VolumePolicy.WHOLE, volume_closed=False, novel_finished=True).verdict
        is ExportVerdict.WAIT
    )
    assert (
        decide_export(VolumePolicy.WHOLE, volume_closed=True, novel_finished=True).verdict
        is ExportVerdict.EXPORT
    )


@pytest.mark.req("EF-BIB-040")
@pytest.mark.parametrize("policy", list(VolumePolicy))
@pytest.mark.parametrize("read", [True, False])
@pytest.mark.parametrize("suspect", [True, False])
def test_a_closed_volume_served_is_never_replaced(
    policy: VolumePolicy, read: bool, suspect: bool
) -> None:
    decision = decide_replacement(
        policy, volume_closed=True, served=True, read=read, skip_read=False, suspect=suspect
    )
    assert decision.verdict is ReplaceVerdict.KEEP_FROZEN


@pytest.mark.req("EF-BIB-040")
@pytest.mark.parametrize("policy", [VolumePolicy.CLOSED_ONLY, VolumePolicy.WHOLE])
def test_only_rolling_tail_replaces_an_open_volume(policy: VolumePolicy) -> None:
    decision = decide_replacement(
        policy, volume_closed=False, served=True, read=False, skip_read=True, suspect=False
    )
    assert decision.verdict is ReplaceVerdict.KEEP_POLICY


@pytest.mark.req("EF-BIB-040")
def test_rolling_tail_replacement_rules() -> None:
    def decide(**facts: bool) -> ReplaceVerdict:
        base = {"served": True, "read": False, "skip_read": True, "suspect": False}
        return decide_replacement(
            VolumePolicy.ROLLING_TAIL, volume_closed=False, **{**base, **facts}
        ).verdict

    assert decide(served=False) is ReplaceVerdict.FIRST_WRITE
    assert decide() is ReplaceVerdict.REPLACE
    assert decide(read=True) is ReplaceVerdict.KEEP_READ  # a finished tail is not rewritten
    assert decide(read=True, skip_read=False) is ReplaceVerdict.REPLACE
    assert decide(suspect=True) is ReplaceVerdict.REFUSE_SUSPECT


@pytest.mark.req("EF-BIB-040")
def test_default_volume_size_cuts_chapters_by_fifty() -> None:
    assert DEFAULT_VOLUME_SIZE == 50
    cut = volume_slices(120)
    assert [(s.start, s.stop, s.closed) for s in cut] == [
        (0, 50, True), (50, 100, True), (100, 120, False),
    ]  # fmt: skip
    assert volume_slices(120, finished=True)[-1].closed  # the novel ended: its last volume too
    assert volume_slices(0) == []
    for bad in (0, -1):
        with pytest.raises(ValueError, match="at least one"):
            volume_slices(10, bad)
    with pytest.raises(ValueError, match="negative"):
        volume_slices(-1)


@pytest.mark.req("EF-BIB-040")
def test_unknown_policy_is_refused_never_defaulted() -> None:
    assert parse_policy("rolling_tail") is VolumePolicy.ROLLING_TAIL
    for bad in ("", "Closed_Only", "everything", "whole "):
        with pytest.raises(ValueError, match="not a volume policy"):
            parse_policy(bad)
