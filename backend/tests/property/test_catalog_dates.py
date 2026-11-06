"""EF-BIB-047 properties: round trip and no exported field finer than the data."""

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shelfhold.catalog.domain.dates import (
    DatePrecision,
    PartialDate,
    comicinfo_date_fields,
    opf_date,
    parse_partial_date,
)
from shelfhold.plugin_host.adapters.meta.suwayomi.service import _upload_date

_NOW = datetime(2030, 1, 1, tzinfo=UTC)


def upload_date(value: object) -> datetime | None:
    return _upload_date(value, _NOW)


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


@pytest.mark.req("EF-BIB-047")
@given(partial_dates(), partial_dates())
def test_partial_date_order_is_the_calendar_order(a: PartialDate, b: PartialDate) -> None:
    ka = (a.year, a.month or 0, a.day or 0)
    kb = (b.year, b.month or 0, b.day or 0)
    assert (a < b) == (ka < kb)
    assert (a <= b) == (ka <= kb)
    assert (a == b) == (ka == kb)


@pytest.mark.req("EF-BIB-047")
@given(partial_dates(), st.sampled_from(list(DatePrecision)))
def test_coarsened_to_never_adds_precision(date: PartialDate, precision: DatePrecision) -> None:
    order = list(DatePrecision)
    if order.index(precision) > order.index(date.precision):
        with pytest.raises(ValueError, match="precision"):
            date.coarsened_to(precision)
        return
    coarse = date.coarsened_to(precision)
    assert coarse.precision is precision
    assert coarse.year == date.year
    assert coarse <= date
    assert coarse.coarsened_to(precision) == coarse
    assert date.iso().startswith(coarse.iso())


@pytest.mark.req("EF-BIB-047")
@given(st.one_of(st.floats(), st.integers(-(10**20), 10**20), st.text(max_size=12)))
def test_upload_date_never_raises(value: object) -> None:
    result = upload_date(value)
    assert result is None or result.tzinfo is not None
