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


def test_a_unit_needs_the_next_one_entered_unless_it_is_the_last() -> None:
    assert traversed_units(chain(1, 3, 5, 7, 9, 10), SPANS, mode=PAGES) == []
    assert traversed_units(chain(1, 3, 5, 7, 9, 10, 11), SPANS, mode=PAGES) == ["a"]
    assert traversed_units(chain(21, 24, 27, 30), SPANS, mode=PAGES) == ["c"]


def test_a_jump_marks_nothing() -> None:
    assert traversed_units(chain(1, 30), SPANS, mode=PAGES) == []
    assert traversed_units(chain(30, 3), SPANS, mode=PAGES) == []


def test_a_gap_above_three_pages_breaks_the_chain() -> None:
    assert traversed_units(chain(1, 4, 7, 10, 14, 17, 20), SPANS, mode=PAGES) == []
    assert traversed_units(chain(1, 4, 7, 10, 13, 16, 19, 20, 21), SPANS, mode=PAGES) == ["a", "b"]


def test_the_chain_must_start_at_the_first_pages_of_the_unit() -> None:
    assert traversed_units(chain(5, 7, 9, 10, 11), SPANS, mode=PAGES) == []


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
