"""EF-EXP-053 / EF-BIB-044: watermark, per-cycle caps, backfill thresholds, what may be requested."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shelfhold.acquisition.domain.backfill import (
    CONFIRM_BYTES,
    CONFIRM_UNITS,
    needs_confirmation,
)
from shelfhold.acquisition.domain.requests import KnownUnit, RequestBudget, requestable
from shelfhold.acquisition.domain.watermark import Watermark


def _unit(
    number: str | None, *, ordinal: int = 0, present: bool = False, **flags: bool
) -> KnownUnit:
    return KnownUnit(
        unit_id=f"u-{ordinal}-{number}",
        ordinal=ordinal,
        number=None if number is None else Decimal(number),
        present=present,
        **flags,
    )


@pytest.mark.req("EF-EXP-053")
def test_watermark_is_the_pair_segment_then_number() -> None:
    mark = Watermark.of("S1", "50")
    assert mark.is_beyond(ordinal=1, number=Decimal("50.5"))
    assert not mark.is_beyond(ordinal=1, number=Decimal("50"))
    assert not mark.is_beyond(ordinal=1, number=Decimal("49"))
    # a more recent segment is beyond whatever its number (I34): S2 chapter 1 follows S1 chapter 50
    assert mark.is_beyond(ordinal=2, number=Decimal("1"))
    assert not Watermark.of("S2", "3").is_beyond(ordinal=1, number=Decimal("900"))
    assert Watermark.of("", "10").is_beyond(ordinal=0, number=Decimal("11"))
    assert Watermark.of("", "10").ordinal == 0
    with pytest.raises(ValueError, match="segment"):
        Watermark.of("season two", "1")


@pytest.mark.req("EF-EXP-053")
def test_requests_capped_per_cycle_and_limit_key() -> None:
    budget = RequestBudget(service_cap=50, key_cap=10)
    assert budget.take("src:a", 4) == 4
    assert budget.take("src:a", 20) == 6  # the key has ten in all: the rest waits
    assert budget.take("src:a", 1) == 0
    for key in ("src:b", "src:c", "src:d", "src:e"):
        assert budget.take(key, 10) == 10
    assert budget.take("src:f", 10) == 0  # the service reached fifty
    assert budget.remaining == 0
    assert RequestBudget(service_cap=50, key_cap=10).take("src:a", 0) == 0


@pytest.mark.req("EF-EXP-053")
def test_the_per_series_cap_counts_units_too() -> None:
    budget = RequestBudget(service_cap=50, key_cap=10)
    assert budget.take("src:a", 8, series_cap=3) == 3


@pytest.mark.req("EF-EXP-053")
def test_backfill_requires_estimate_and_confirmation_over_100_units_or_5gib() -> None:
    assert (CONFIRM_UNITS, CONFIRM_BYTES) == (100, 5 * 1024**3)
    assert not needs_confirmation(units=100, size_bytes=5 * 1024**3)
    assert needs_confirmation(units=101, size_bytes=1)
    assert needs_confirmation(units=1, size_bytes=5 * 1024**3 + 1)


@pytest.mark.req("EF-EXP-053")
def test_follow_requests_only_what_is_beyond_the_watermark() -> None:
    units = [_unit(str(n)) for n in range(1, 8)]
    mark = Watermark.of("", "5")
    assert [u.number for u in requestable(units, mark, "follow")] == [Decimal(6), Decimal(7)]
    assert requestable(units, None, "follow") == []  # no watermark yet: nothing is "new"


@pytest.mark.req("EF-EXP-053")
def test_gaps_are_the_holes_below_the_watermark_and_all_is_the_history() -> None:
    units = [_unit(str(n), present=n in (3, 4, 8)) for n in range(1, 11)]
    mark = Watermark.of("", "8")
    assert [int(u.number or 0) for u in requestable(units, mark, "gaps")] == [5, 6, 7]
    assert [int(u.number or 0) for u in requestable(units, mark, "all")] == [1, 2, 5, 6, 7]
    assert requestable([_unit("1"), _unit("2")], mark, "gaps") == []  # no file: no hole


@pytest.mark.req("EF-EXP-053")
def test_a_suppressed_acknowledged_or_already_asked_unit_is_never_requested() -> None:
    units = [
        _unit("1", suppressed=True),
        _unit("2", acknowledged=True),
        _unit("3", ticketed=True),
        _unit("4", live=False),
        _unit("5"),
    ]
    assert [u.number for u in requestable(units, Watermark.of("", "0"), "follow")] == [
        Decimal(5)
    ]


@pytest.mark.req("EF-BIB-044")
@given(
    present=st.sets(st.integers(1, 40)),
    mark=st.integers(0, 40),
    mode=st.sampled_from(["follow", "gaps", "all"]),
)
def test_no_cycle_requests_covered_unit(present: set[int], mark: int, mode: str) -> None:
    """Whatever mixes volumes and chapters, a Unit that is present is never asked for."""
    units = [_unit(str(n), present=n in present) for n in range(1, 41)]
    for chosen in requestable(units, Watermark.of("", str(mark)), mode):  # type: ignore[arg-type]
        assert not chosen.present
