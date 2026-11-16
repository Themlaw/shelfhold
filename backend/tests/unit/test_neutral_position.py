"""M4 L15: the neutral position, and how a page of a volume becomes a chapter (EF-BIB-026,
EF-BIB-044). Pure rules, no database: the transfer over real tables is in
`test_m4_l15_edition_change.py`."""

import pytest

from shelfhold.reading.domain.neutral_position import (
    NeutralPosition,
    SourcePart,
    neutral_of,
    transpose,
)

pytestmark = pytest.mark.req("EF-BIB-026", "EF-BIB-044")


def test_a_neutral_position_is_always_approximate() -> None:
    position = NeutralPosition("w", 3, 0.5, False)
    assert position.approximate is True
    with pytest.raises(ValueError, match="approximate"):
        NeutralPosition("w", 3, 0.5, False, approximate=False)


@pytest.mark.parametrize("total", [-0.1, 1.1])
def test_a_total_progression_stays_between_zero_and_one(total: float) -> None:
    with pytest.raises(ValueError, match="totalProgression"):
        NeutralPosition("w", 1, total, False)


def test_the_furthest_part_reached_is_the_neutral_position() -> None:
    parts = [
        SourcePart(1.0, "read", 1.0),
        SourcePart(2.0, "read", 1.0),
        SourcePart(3.0, "in_progress", 0.5),
        SourcePart(4.0, "unread", None),
    ]
    got = neutral_of("w", parts)
    assert got is not None
    assert (got.work_id, got.part_ordinal, got.completed) == ("w", 3, False)
    assert got.total_progression == pytest.approx(2.5 / 4)
    assert (got.part_number, got.part_progression) == (3.0, 0.5)


def test_a_whole_read_work_is_completed() -> None:
    parts = [SourcePart(1.0, "read", 1.0), SourcePart(2.0, "read", 1.0)]
    got = neutral_of("w", parts)
    assert got is not None and got.completed is True and got.total_progression == 1.0


def test_nothing_read_is_no_position() -> None:
    assert neutral_of("w", [SourcePart(1.0, "unread", None)]) is None
    assert neutral_of("w", []) is None


def test_a_part_with_no_known_share_counts_for_its_start_never_beyond() -> None:
    got = neutral_of("w", [SourcePart(1.0, "in_progress", None), SourcePart(2.0, "unread", None)])
    assert got is not None and got.total_progression == 0.0


def test_the_same_number_in_the_other_edition_is_the_same_part() -> None:
    position = NeutralPosition("w", 3, 0.5, False, part_number=40.0, part_progression=0.25)
    assert transpose(position, [38.0, 39.0, 40.0, 41.0]) == (2, 0.25)


def test_without_the_same_number_the_share_of_the_whole_decides() -> None:
    position = NeutralPosition("w", 3, 0.6, False, part_number=40.0)
    landed = transpose(position, [1.0, 2.0, 3.0, 4.0, 5.0])  # no 40 there
    assert landed is not None
    assert landed[0] == 3 and landed[1] == pytest.approx(0.0, abs=1e-9)


def test_a_completed_work_lands_on_the_last_part_whole() -> None:
    position = NeutralPosition("w", 5, 1.0, True, part_number=5.0)
    assert transpose(position, [1.0, 2.0, 3.0]) == (2, 1.0)


def test_nothing_to_transpose_onto_an_edition_with_no_part() -> None:
    assert transpose(NeutralPosition("w", 1, 0.2, False), []) is None
