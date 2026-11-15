"""The merge rule of reading positions and the projection of a file position on the Units it
covers (EF-LEC-014, EF-LEC-007, ADR-0053 "une seule règle de position"). Pure functions: the SQL
view `reading_progress` is proved equal to them in `tests/integration/test_reading_progress.py`."""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.reading.domain.merge import Observation, bounded_ts, furthest
from shelfhold.reading.domain.projection import Bounds, Projected, State, project

T = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def seen(
    pid: str, state: State, percent: float | None = None, *, client: float = 0, server: float = 0
) -> Observation:
    return Observation(
        pid,
        Projected(state, False),
        percent,
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


@pytest.mark.req("EF-LEC-014")
def test_same_state_the_furthest_percent_then_the_latest_bounded_date() -> None:
    low = seen("low", "in_progress", 0.2, client=9, server=9)
    high = seen("high", "in_progress", 0.7, client=1, server=1)
    assert furthest([low, high]).position_id == "high"
    tie_old = seen("old", "in_progress", 0.5, client=1, server=1)
    tie_new = seen("new", "in_progress", 0.5, client=2, server=2)
    assert furthest([tie_old, tie_new]).position_id == "new"
    liar = seen("liar", "in_progress", 0.5, client=900, server=3)  # bounded: 3, not 900
    assert furthest([liar, tie_new]).position_id == "liar"  # 3 > 2 once bounded, not by 900
    assert (
        furthest([seen("x", "in_progress", 0.5, client=900, server=1), tie_new]).position_id
        == "new"
    )


@pytest.mark.req("EF-LEC-014")
def test_furthest_of_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="no position"):
        furthest([])


@pytest.mark.req("EF-LEC-014")
def test_exact_projection_with_known_pages() -> None:
    bounds = Bounds(start_page=10, end_page=20, rank0=1, count=3)
    assert project(page=9, percent=0.1, bounds=bounds) == Projected("unread", False)
    assert project(page=10, percent=0.1, bounds=bounds) == Projected("in_progress", False)
    assert project(page=20, percent=0.9, bounds=bounds) == Projected("read", False)


@pytest.mark.req("EF-LEC-014")
def test_proportional_projection_is_approximate_unless_one_unit() -> None:
    bounds = Bounds(start_page=None, end_page=None, rank0=1, count=3)
    assert project(page=None, percent=0.5, bounds=bounds) == Projected("in_progress", True)
    assert project(page=None, percent=0.67, bounds=bounds) == Projected("read", True)
    alone = Bounds(None, None, rank0=0, count=1)
    assert project(page=None, percent=1.0, bounds=alone) == Projected("read", False)
    assert project(page=None, percent=None, bounds=alone) == Projected("unread", True)


@pytest.mark.req("EF-LEC-014", "EF-BIB-026")
def test_replaced_content_ignores_the_page_and_is_approximate() -> None:
    """The page was counted in the old file: the new bounds do not apply to it."""
    bounds = Bounds(start_page=10, end_page=20, rank0=0, count=2)
    got = project(page=15, percent=0.9, bounds=bounds, replaced=True)
    assert got == Projected("read", True)
