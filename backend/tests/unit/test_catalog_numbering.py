"""EF-BIB-002 / EF-BIB-050: unit numbering — tracks, parts, segments, total order."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from shelfhold.catalog.domain.numbering import (
    NUMBER_NORMALIZER_ID,
    NumberError,
    Track,
    parse_number,
)
from tests.support.numbering import follow_from, is_beyond, unit_sort_key


@pytest.mark.req("EF-BIB-002")
def test_number_sort_key_decimal_and_text() -> None:
    assert parse_number("10.5").sort == Decimal("10.5")
    assert parse_number("0").sort == Decimal(0)
    assert parse_number("Ch. 12").sort == Decimal(12)
    assert parse_number("10,5").norm == "10.5"
    assert parse_number("010.50").norm == "10.5"
    extra = parse_number("Extra")
    assert extra.track is Track.EXTRA
    assert extra.norm == ""
    assert parse_number(None).track is Track.UNNUMBERED
    assert parse_number("???").track is Track.UNNUMBERED


@pytest.mark.req("EF-BIB-002")
def test_total_order_track_number_part_source_order() -> None:
    raws = ["Annual 1", "10 part 2", "10.5", "10", "Extra", "9"]
    units = [parse_number(r, source_order=i) for i, r in enumerate(raws)]
    ordered = [u.raw for u in sorted(units, key=unit_sort_key)]
    assert ordered == ["9", "10", "10 part 2", "10.5", "Annual 1", "Extra"]


@pytest.mark.req("EF-BIB-002")
def test_total_order_ties_broken_by_source_order_then_date_then_remote_id() -> None:
    first = parse_number("5", source_order=1)
    second = parse_number("5", source_order=2)
    assert unit_sort_key(first) < unit_sort_key(second)
    early = datetime(2020, 1, 1, tzinfo=UTC)
    late = datetime(2021, 1, 1, tzinfo=UTC)
    assert unit_sort_key(first, early) < unit_sort_key(first, late)
    assert unit_sort_key(first, late) < unit_sort_key(first, None)
    assert unit_sort_key(first, early, "a") < unit_sort_key(first, early, "b")


@pytest.mark.req("EF-BIB-002")
def test_unnumbered_extras_ordered_by_source_order() -> None:
    units = [parse_number("Extra", source_order=n) for n in (3, 1, 2)]
    assert [u.source_order for u in sorted(units, key=unit_sort_key)] == [1, 2, 3]


@pytest.mark.req("EF-BIB-002")
@pytest.mark.parametrize(
    ("raw", "track", "norm"),
    [
        ("Annual 1", Track.ANNUAL, "1"),
        ("annual 2", Track.ANNUAL, "2"),
        ("Annuel 3", Track.ANNUAL, "3"),
        ("Special 4", Track.SPECIAL, "4"),
        ("Hors-série 1", Track.SPECIAL, "1"),
        ("One-shot", Track.ONESHOT, ""),
        ("Oneshot", Track.ONESHOT, ""),
        ("12", Track.MAIN, "12"),
    ],
)
def test_annual_maps_to_track(raw: str, track: Track, norm: str) -> None:
    parsed = parse_number(raw)
    assert (parsed.track, parsed.norm) == (track, norm)


@pytest.mark.req("EF-BIB-002")
def test_track_hint_wins_for_a_bare_number() -> None:
    assert parse_number("3", track_hint=Track.SPECIAL).track is Track.SPECIAL


@pytest.mark.req("EF-BIB-050")
def test_number_segment_ordinal_orders_s10_after_s2() -> None:
    s2 = parse_number("S2 Ch.40")
    s10 = parse_number("S10 Ch.1")
    assert (s2.segment, s2.segment_ordinal, s2.sort) == ("S2", 2, Decimal(40))
    assert (s10.segment, s10.segment_ordinal) == ("S10", 10)
    assert "S10" < "S2"  # the text order is wrong, which is the point of the ordinal
    assert unit_sort_key(s2) < unit_sort_key(s10)
    assert is_beyond(s10, follow_from(s2))
    assert not is_beyond(s2, follow_from(s10))
    assert parse_number("Season 3 Chapter 4").segment == "S3"
    assert parse_number("Saison 3 ch 4").segment == "S3"
    assert parse_number("S2E5").sort == Decimal(5)
    plain = parse_number("7")
    assert (plain.segment, plain.segment_ordinal) == ("", 0)


@pytest.mark.req("EF-BIB-050")
def test_follow_from_pair_segment_number_example() -> None:
    mark = follow_from(parse_number("S2 Ch.40"))
    assert is_beyond(parse_number("S10 Ch.1"), mark)
    assert is_beyond(parse_number("S2 Ch.41"), mark)
    assert not is_beyond(parse_number("S2 Ch.40"), mark)
    assert not is_beyond(parse_number("S1 Ch.99"), mark)


@pytest.mark.req("ENF-OPS-021")
def test_parsed_number_keeps_raw_and_normalizer_version() -> None:
    parsed = parse_number(" Ch. 012 ")
    assert parsed.raw == " Ch. 012 "
    assert parsed.normalized_by == NUMBER_NORMALIZER_ID
    assert NUMBER_NORMALIZER_ID == "number@1"


@pytest.mark.req("EF-BIB-002")
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("1" * 19, "too_long"),
        ("1" * 40, "too_long"),
        ("0.1234567", "too_precise"),
        ("1." + "0" * 27 + "1", "too_precise"),
        ("Ch.-5", "negative"),
        ("-3", "negative"),
        ("\u22123", "negative"),
    ],
)
def test_out_of_bounds_numbers_are_refused_with_a_typed_error(raw: str, code: str) -> None:
    with pytest.raises(NumberError) as err:
        parse_number(raw)
    assert err.value.code == code


@pytest.mark.req("EF-BIB-002")
def test_bounds_are_inclusive_and_padding_is_free() -> None:
    assert parse_number("9" * 18).norm == "9" * 18
    assert parse_number("0" * 30 + "7").norm == "7"
    assert parse_number("1.500000").norm == "1.5"
    assert parse_number("1.1234560000000000000").norm == "1.123456"
    near = [parse_number(f"1.{i:06d}") for i in (1, 2)]
    assert near[0].sort != near[1].sort  # no collision at the last accepted decimal
    assert parse_number("Hors-série 1").norm == "1"  # a hyphen inside a word is not a sign
    assert parse_number("10-12").norm == "10"


@pytest.mark.req("EF-BIB-002")
def test_ties_are_broken_by_source_order_not_by_the_number() -> None:
    """Different places that share one sort key: only `source_order` separates them."""
    s0 = parse_number("S0 Ch.5", source_order=2)
    plain = parse_number("5", source_order=1)
    assert (s0.segment, plain.segment) == ("S0", "")  # two distinct places...
    assert unit_sort_key(s0)[:4] == unit_sort_key(plain)[:4]  # ...with one number-level key
    assert [u.source_order for u in sorted([s0, plain], key=unit_sort_key)] == [1, 2]

    extra = parse_number("Extra", source_order=4)
    extra0 = parse_number("Extra 0", source_order=3)
    assert (extra.norm, extra0.norm) == ("", "0")
    assert unit_sort_key(extra)[:4] == unit_sort_key(extra0)[:4]
    assert [u.source_order for u in sorted([extra, extra0], key=unit_sort_key)] == [3, 4]
