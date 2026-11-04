"""EF-BIB-002 / EF-BIB-050 properties: stable, total, collision-free order."""

import random
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from shelfhold.catalog.domain.numbering import (
    NumberError,
    Track,
    follow_from,
    is_beyond,
    parse_number,
    unit_sort_key,
)

decimals = st.decimals(min_value=0, max_value=10**17, places=6, allow_nan=False)
RAW_TEXT = st.one_of(
    decimals.map(lambda d: format(d, "f")),
    st.sampled_from(["Extra", "Annual 1", "10 part 2", "Special 3", "One-shot", "S2 Ch.4", "??"]),
    st.text(max_size=12),
)


@pytest.mark.req("EF-BIB-002")
@given(decimals, decimals)
def test_number_sort_key_decimal_and_text_property(a: Decimal, b: Decimal) -> None:
    ua, ub = parse_number(format(a, "f")), parse_number(format(b, "f"))
    assert ua.sort == a
    assert (ua.sort < ub.sort) == (a < b)
    assert (unit_sort_key(ua) < unit_sort_key(ub)) == (a < b)


@pytest.mark.req("EF-BIB-002")
@given(st.lists(RAW_TEXT, min_size=1, max_size=15), st.randoms(use_true_random=False))
@example(["-0"], random.Random(0))  # noqa: S311  (a negative number is refused on purpose)
def test_total_order_is_stable_and_collision_free(raws: list[str], rnd: random.Random) -> None:
    base = datetime(2020, 1, 1, tzinfo=UTC)
    try:
        units = [
            (parse_number(r, source_order=i), base if i % 2 else None, f"remote-{i}")
            for i, r in enumerate(raws)
        ]
    except NumberError:
        assume(False)
    keys = [unit_sort_key(u, d, rid) for u, d, rid in units]
    assert len(set(keys)) == len(keys)  # distinct remote ids never collide
    shuffled = units[:]
    rnd.shuffle(shuffled)

    def order(items: list) -> list[str]:  # type: ignore[type-arg]
        return [rid for _, _, rid in sorted(items, key=lambda t: unit_sort_key(*t))]

    assert order(shuffled) == order(units)


@pytest.mark.req("EF-BIB-002")
@given(st.text(max_size=40))
def test_parse_number_raises_only_typed_errors_and_is_idempotent(raw: str) -> None:
    try:
        parsed = parse_number(raw)
    except NumberError:
        return
    assert parsed.raw == raw
    if parsed.norm != "":
        again = parse_number(parsed.norm, track_hint=parsed.track)
        assert again.norm == parsed.norm
        assert again.sort == parsed.sort


@pytest.mark.req("EF-BIB-002")
@given(st.integers(0, 10**18 - 1), st.integers(0, 10**6 - 1))
def test_decimals_within_bounds_are_exact(whole: int, fraction: int) -> None:
    raw = f"{whole}.{fraction:06d}"
    parsed = parse_number(raw)
    assert parsed.sort == Decimal(raw)
    assert Decimal(parsed.norm) == Decimal(raw)


@pytest.mark.req("EF-BIB-050")
@given(
    st.integers(1, 40),
    decimals,
    st.integers(1, 40),
    decimals,
)
def test_follow_from_pair_segment_number(
    seg_a: int, num_a: Decimal, seg_b: int, num_b: Decimal
) -> None:
    a = parse_number(f"S{seg_a} Ch.{num_a:f}")
    b = parse_number(f"S{seg_b} Ch.{num_b:f}")
    expected = (seg_b, num_b) > (seg_a, num_a)
    assert is_beyond(b, follow_from(a)) == expected
    # the watermark agrees with the total order on the same track
    assert is_beyond(b, follow_from(a)) == (unit_sort_key(b)[:3] > unit_sort_key(a)[:3])


@pytest.mark.req("EF-BIB-050")
@given(st.integers(1, 99), st.integers(1, 99))
def test_number_segment_ordinal_orders_numerically(x: int, y: int) -> None:
    ux, uy = parse_number(f"S{x} Ch.1"), parse_number(f"S{y} Ch.1")
    assert (ux.segment_ordinal < uy.segment_ordinal) == (x < y)
    assert ux.track is Track.MAIN
