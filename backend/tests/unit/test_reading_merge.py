"""The merge rule of reading positions and the projection of a file position on the Units it
covers (EF-LEC-014, EF-LEC-007, ADR-0053 "une seule règle de position"). Pure functions: the SQL
view `reading_progress` is proved equal to them in `tests/integration/test_reading_progress.py`."""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.reading.domain.merge import Observation, bounded_ts, furthest, latest
from shelfhold.reading.domain.projection import Bounds, Projected, State, project

T = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def seen(
    pid: str,
    state: State,
    within: float | None = None,
    *,
    approximate: bool = False,
    client: float = 0,
    server: float = 0,
) -> Observation:
    return Observation(
        pid,
        Projected(state, approximate, within),
        T + timedelta(hours=client),
        T + timedelta(hours=server),
    )


@pytest.mark.req("EF-LEC-007")
def test_client_ts_is_bounded_by_server_ts() -> None:
    assert bounded_ts(T + timedelta(days=400), T) == T  # a date in the future is the server's
    assert bounded_ts(T - timedelta(hours=2), T) == T - timedelta(hours=2)


@pytest.mark.req("EF-LEC-014")
def test_furthest_state_wins_over_the_latest_one() -> None:
    older_read = seen("a", "read", 1.0, client=0, server=0)
    newer_start = seen("b", "unread", 0.0, client=5, server=5)  # reopened at the beginning
    assert furthest([newer_start, older_read]).position_id == "a"
    assert latest([newer_start, older_read]).position_id == "b"  # where the reader is now


@pytest.mark.req("EF-LEC-014")
def test_same_state_exact_first_then_the_fraction_of_the_unit_then_the_bounded_date() -> None:
    low = seen("low", "in_progress", 0.2, client=9, server=9)
    high = seen("high", "in_progress", 0.7, client=1, server=1)
    assert furthest([low, high]).position_id == "high"
    guess = seen("guess", "in_progress", 0.9, approximate=True, client=9, server=9)
    assert furthest([guess, low]).position_id == "low"  # exact before approximate
    no_share = seen("none", "in_progress", None, client=9, server=9)
    assert furthest([no_share, low]).position_id == "low"  # nothing known ranks below anything
    tie_old = seen("old", "in_progress", 0.5, client=1, server=1)
    tie_new = seen("new", "in_progress", 0.5, client=2, server=2)
    assert furthest([tie_old, tie_new]).position_id == "new"
    ahead = seen("ahead", "in_progress", 0.5, client=900, server=3)  # bounded: 3, not 900
    assert furthest([ahead, tie_new]).position_id == "ahead"
    assert (
        furthest([seen("x", "in_progress", 0.5, client=900, server=1), tie_new]).position_id
        == "new"
    )


@pytest.mark.req("EF-LEC-014")
def test_furthest_and_latest_of_nothing_are_errors() -> None:
    with pytest.raises(ValueError, match="no position"):
        furthest([])
    with pytest.raises(ValueError, match="no position"):
        latest([])


@pytest.mark.req("EF-LEC-014")
def test_exact_projection_with_known_pages() -> None:
    bounds = Bounds(start_page=10, end_page=20, rank0=1, count=3)
    assert project(page=9, percent=0.1, bounds=bounds) == Projected("unread", False, 0.0)
    assert project(page=10, percent=0.1, bounds=bounds) == Projected("in_progress", False, 0.0)
    assert project(page=15, percent=None, bounds=bounds).within == 0.5
    assert project(page=20, percent=0.9, bounds=bounds) == Projected("read", False, 1.0)
    one_page = Bounds(start_page=5, end_page=5, rank0=0, count=1)
    assert project(page=5, percent=None, bounds=one_page).within == 1.0


@pytest.mark.req("EF-LEC-014")
def test_proportional_projection_is_approximate_unless_one_unit() -> None:
    bounds = Bounds(start_page=None, end_page=None, rank0=1, count=3)
    got = project(page=None, percent=0.5, bounds=bounds)
    assert (got.state, got.approximate) == ("in_progress", True)
    assert got.within == pytest.approx(0.5)  # 0.5 of the file is half of the second third
    assert project(page=None, percent=0.67, bounds=bounds).state == "read"
    alone = Bounds(None, None, rank0=0, count=1)
    assert project(page=None, percent=1.0, bounds=alone) == Projected("read", False, 1.0)
    assert project(page=None, percent=None, bounds=alone) == Projected("unread", True, None)


@pytest.mark.req("EF-LEC-014", "EF-BIB-026")
def test_replaced_content_ignores_the_page_and_is_approximate() -> None:
    """The page was counted in a file that is no longer there: the new bounds do not apply."""
    bounds = Bounds(start_page=10, end_page=20, rank0=0, count=2)
    got = project(page=15, percent=0.9, bounds=bounds, replaced=True)
    assert (got.state, got.approximate) == ("read", True)


@pytest.mark.req("EF-LEC-014")
def test_a_device_report_never_says_read_by_itself() -> None:
    """A look at the last page is a position, not a reading (ADR-0053)."""
    bounds = Bounds(start_page=21, end_page=30, rank0=2, count=3)
    assert project(page=30, percent=1.0, bounds=bounds, trusted=False).state == "in_progress"
    assert project(page=5, percent=0.1, bounds=bounds, trusted=False).state == "unread"
    assert project(page=30, percent=1.0, bounds=bounds, trusted=True).state == "read"
