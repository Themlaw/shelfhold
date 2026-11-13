"""M3 L26 (EF-BIB-030): the « Ajouter » sheet compares the estimated size with the free space
less the reserve, and refuses before any write, with the figures and the action."""

import pytest
from shelfhold.library.application.disk_guard import (
    EMPTY_TRASH_ACTION,
    GIB,
    AddSpaceRefusedError,
    check_add_fits,
    reserve_for,
)

from shelfhold.infrastructure.observability.disk_guard import DiskThresholds

pytestmark = pytest.mark.req("EF-BIB-030")


def test_a_series_of_1000_chapters_over_the_free_space_is_refused_with_the_figures() -> None:
    with pytest.raises(AddSpaceRefusedError) as refused:
        check_add_fits(60 * GIB, free_bytes=40 * GIB, reserve_bytes=10 * GIB)
    error = refused.value
    assert (error.estimated_bytes, error.free_bytes, error.reserve_bytes) == (
        60 * GIB, 40 * GIB, 10 * GIB,
    )  # fmt: skip
    assert error.reclaim_action == EMPTY_TRASH_ACTION == "empty_trash"
    assert error.code == "library.space_insufficient"


def test_the_reserve_counts_not_only_the_free_space() -> None:
    """35 GiB would fit in 40 GiB free, but not once the 10 GiB reserve is kept."""
    with pytest.raises(AddSpaceRefusedError):
        check_add_fits(35 * GIB, free_bytes=40 * GIB, reserve_bytes=10 * GIB)
    assert check_add_fits(30 * GIB, free_bytes=40 * GIB, reserve_bytes=10 * GIB).usable_bytes == (
        30 * GIB
    )


def test_the_exact_edge_fits_and_one_byte_more_does_not() -> None:
    check_add_fits(30 * GIB, free_bytes=40 * GIB, reserve_bytes=10 * GIB)
    with pytest.raises(AddSpaceRefusedError):
        check_add_fits(30 * GIB + 1, free_bytes=40 * GIB, reserve_bytes=10 * GIB)


def test_a_disk_already_under_its_reserve_refuses_every_addition_but_an_empty_one() -> None:
    with pytest.raises(AddSpaceRefusedError) as refused:
        check_add_fits(1, free_bytes=5 * GIB, reserve_bytes=10 * GIB)
    assert refused.value.usable_bytes == 0  # never negative
    assert check_add_fits(0, free_bytes=5 * GIB, reserve_bytes=10 * GIB).estimated_bytes == 0


@pytest.mark.parametrize("bad", [-1, 1.5, "10", None, True])
def test_a_size_that_is_not_a_natural_number_is_refused_as_input(bad: object) -> None:
    with pytest.raises(ValueError):
        check_add_fits(bad, free_bytes=GIB, reserve_bytes=0)  # type: ignore[arg-type]


def test_the_reserve_is_the_pause_mark_of_the_disk_guard() -> None:
    marks = DiskThresholds()
    assert reserve_for(100 * GIB, marks) == 10 * GIB  # max(5 %, 10 GiB)
    assert reserve_for(1000 * GIB, marks) == 50 * GIB
