"""The merge of reading positions and the projection of a file position on the Units it covers
(EF-LEC-014, EF-LEC-007, ADR-0053 "une seule règle de position"). The furthest-position rule
itself is the SQL view `reading_progress`, proved in `tests/integration/test_reading_progress.py`;
what is left here is the bounded date and the projection, pure functions."""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.reading.domain.merge import bounded_ts
from shelfhold.reading.domain.projection import Bounds, Projected, project

T = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


@pytest.mark.req("EF-LEC-007")
def test_client_ts_is_bounded_by_server_ts() -> None:
    assert bounded_ts(T + timedelta(days=400), T) == T  # a date in the future is the server's
    assert bounded_ts(T - timedelta(hours=2), T) == T - timedelta(hours=2)


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
