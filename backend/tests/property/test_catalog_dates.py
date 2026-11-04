"""EF-BIB-047 properties: round trip and no exported field finer than the data."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shelfhold.catalog.domain.dates import (
    PartialDate,
    comicinfo_date_fields,
    opf_date,
    parse_partial_date,
)


@st.composite
def partial_dates(draw: st.DrawFn) -> PartialDate:
    year = draw(st.integers(1, 9999))
    month = draw(st.one_of(st.none(), st.integers(1, 12)))
    day = draw(st.integers(1, 28)) if month is not None and draw(st.booleans()) else None
    return PartialDate(year, month, day)


@pytest.mark.req("EF-BIB-047")
@given(partial_dates())
def test_partial_dates_never_gain_precision_property(date: PartialDate) -> None:
    assert parse_partial_date(str(date)) == date
    fields = comicinfo_date_fields(date)
    assert set(fields) <= {"Year", "Month", "Day"}
    assert len(fields) == len(date.iso().split("-"))
    assert opf_date(date) == date.iso()
    assert len(opf_date(date).split("-")) == len(fields)
    assert ("Month" in fields) == (date.month is not None)
    assert ("Day" in fields) == (date.day is not None)
