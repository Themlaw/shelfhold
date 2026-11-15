"""When a Unit counts as read: a page-by-page traversal (ADR-0053, MAJ-2, EF-LEC-014)."""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.reading.domain.traversal import PAGES, SHARE, Span, Step, traversed_units

T = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
SPANS = [Span("a", 1, 10), Span("b", 11, 20), Span("c", 21, 30)]


def chain(*xs: float) -> list[Step]:
    return [Step(x, T + timedelta(minutes=i)) for i, x in enumerate(xs)]


pytestmark = pytest.mark.req("EF-LEC-014")


def test_a_chain_through_every_unit_marks_them_all() -> None:
    pages = [1, 3, 5, 7, 9, 10, 11, 13, 15, 17, 19, 20, 21, 23, 25, 27, 29, 30]
    assert traversed_units(chain(*pages), SPANS, mode=PAGES) == ["a", "b", "c"]


def test_a_unit_is_read_once_its_last_page_is_reached() -> None:
    assert traversed_units(chain(1, 3, 5, 7, 9, 10), SPANS, mode=PAGES) == ["a"]
    assert traversed_units(chain(1, 3, 5, 7, 9, 10, 11), SPANS, mode=PAGES) == ["a"]
    assert traversed_units(chain(21, 24, 27, 30), SPANS, mode=PAGES) == ["c"]


def test_a_jump_marks_nothing() -> None:
    assert traversed_units(chain(1, 30), SPANS, mode=PAGES) == []
    assert traversed_units(chain(30, 3), SPANS, mode=PAGES) == []


def test_a_wide_gap_at_a_human_speed_is_a_traversal() -> None:
    """KOReader pushes when the book is closed, a chapter apart: 9 pages in one minute is a
    reading (the old 3 page rule would have marked nothing)."""
    assert traversed_units(chain(1, 10, 11, 20, 21, 30), SPANS, mode=PAGES) == ["a", "b", "c"]


def test_the_same_gap_too_fast_is_a_skip() -> None:
    fast = [Step(1, T), Step(30, T + timedelta(seconds=10))]
    assert traversed_units(fast, SPANS, mode=PAGES) == []


def test_the_speed_limit_is_configurable() -> None:
    slow = [Step(1, T), Step(30, T + timedelta(minutes=2))]  # 14.5 pages a minute
    assert traversed_units(slow, SPANS, mode=PAGES) == ["a", "b", "c"]
    assert traversed_units(slow, SPANS, mode=PAGES, max_pages_per_minute=10) == []


def test_the_client_date_never_counts() -> None:
    # only `ts` (the server date) is read: a Step has no other clock
    assert [f for f in Step.__dataclass_fields__] == ["x", "ts"]


def test_a_small_gap_is_always_admitted_whatever_the_speed() -> None:
    same_second = [Step(x, T) for x in (1, 3, 5, 7, 9, 10, 11)]
    assert traversed_units(same_second, SPANS, mode=PAGES) == ["a"]


def test_the_chain_must_start_at_the_first_pages_of_the_unit() -> None:
    assert traversed_units(chain(5, 7, 9, 10, 11), SPANS, mode=PAGES) == []


def test_a_share_gap_becomes_pages_with_the_page_count() -> None:
    whole = [Span("only", 0.0, 1.0)]
    steps = [
        Step(0.0, T),
        Step(0.5, T + timedelta(minutes=10)),
        Step(1.0, T + timedelta(minutes=20)),
    ]
    assert traversed_units(steps, whole, mode=SHARE) == []  # no page count: only 5 % steps
    assert traversed_units(steps, whole, mode=SHARE, pages_per_unit_of_x=200) == ["only"]
    assert traversed_units(steps, whole, mode=SHARE, pages_per_unit_of_x=2000) == []


def test_an_undone_mark_stays_undone_until_new_evidence() -> None:
    steps = chain(1, 3, 5, 7, 9, 10, 11)
    undone = T + timedelta(hours=1)
    assert traversed_units(steps, SPANS, mode=PAGES, after={"a": undone}) == []
    later = steps + [
        Step(x, undone + timedelta(minutes=i + 1)) for i, x in enumerate([1, 3, 5, 7, 9, 10, 11])
    ]
    assert traversed_units(later, SPANS, mode=PAGES, after={"a": undone}) == ["a"]


def test_a_single_unit_file_is_read_by_a_chain_of_shares() -> None:
    whole = [Span("only", 0.0, 1.0)]
    steps = chain(*[i / 20 for i in range(21)])
    assert traversed_units(steps, whole, mode=SHARE) == ["only"]
    assert traversed_units(chain(0.0, 1.0), whole, mode=SHARE) == []
