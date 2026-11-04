"""EF-BIB-047: partial dates keep their precision and never gain any."""

from datetime import UTC, datetime

import pytest
from shelfhold.catalog.domain.dates import (
    DatePrecision,
    PartialDate,
    comicinfo_date_fields,
    from_upload_timestamp,
    opf_date,
    parse_partial_date,
)


@pytest.mark.req("EF-BIB-047")
def test_partial_dates_never_gain_precision() -> None:
    year = parse_partial_date("2023")
    assert year is not None
    assert year.precision is DatePrecision.YEAR
    assert comicinfo_date_fields(year) == {"Year": 2023}
    assert opf_date(year) == "2023"
    month = parse_partial_date("2023-05")
    assert month is not None
    assert comicinfo_date_fields(month) == {"Year": 2023, "Month": 5}
    assert opf_date(month) == "2023-05"
    day = parse_partial_date("2023-05-14")
    assert day is not None
    assert comicinfo_date_fields(day) == {"Year": 2023, "Month": 5, "Day": 14}
    assert opf_date(day) == "2023-05-14"
    with pytest.raises(ValueError, match="precision"):
        year.coarsened_to(DatePrecision.DAY)
    assert day.coarsened_to(DatePrecision.MONTH) == month


@pytest.mark.req("EF-BIB-047")
@pytest.mark.parametrize("zero", [None, 0, "0", "", "0000", "0000-00-00", "1970-01-01", 0.0])
def test_zero_or_null_upload_date_becomes_null(zero: object) -> None:
    assert parse_partial_date(zero) is None  # type: ignore[arg-type]
    assert from_upload_timestamp(zero) is None  # type: ignore[arg-type]


@pytest.mark.req("EF-BIB-047")
def test_upload_timestamp_seconds_or_milliseconds() -> None:
    expected = datetime(2023, 5, 14, 12, 0, tzinfo=UTC)
    assert from_upload_timestamp(int(expected.timestamp())) == expected
    assert from_upload_timestamp(int(expected.timestamp()) * 1000) == expected
    assert from_upload_timestamp(-5) is None


@pytest.mark.req("EF-BIB-047")
@pytest.mark.parametrize(
    "bad", ["2023-13", "2023-02-30", "23", "2023-5", "abc", "2023-05-14T10:00"]
)
def test_invalid_partial_dates_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="partial date"):
        parse_partial_date(bad)


@pytest.mark.req("EF-BIB-047")
def test_partial_date_invariants() -> None:
    with pytest.raises(ValueError, match="partial date"):
        PartialDate(2023, None, 14)
    assert str(PartialDate(2023, 5)) == "2023-05"
    assert PartialDate(2023) < PartialDate(2023, 5) < PartialDate(2023, 5, 1) < PartialDate(2024)
