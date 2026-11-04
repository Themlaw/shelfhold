"""EF-BIB-002 / EF-BIB-050: unit numbering — tracks, parts, segments, total order."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from shelfhold.catalog.domain.numbering import (
    NUMBER_NORMALIZER_ID,
    Track,
    follow_from,
    is_beyond,
    parse_number,
    unit_sort_key,
)


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
