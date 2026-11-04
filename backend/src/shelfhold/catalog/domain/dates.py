"""Partial dates (EF-BIB-047): partial ISO 8601, never more precise than the data."""

import calendar
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import total_ordering


class DatePrecision(StrEnum):
    YEAR = "year"
    MONTH = "month"
    DAY = "day"


_PARTIAL = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
_ZERO_TEXTS = frozenset({"", "0", "0000", "0000-00", "0000-00-00"})
_MS_THRESHOLD = 100_000_000_000  # above this a Unix timestamp is in milliseconds (year 5138 in s)


@total_ordering
@dataclass(frozen=True)
class PartialDate:
    """Year, optional month, optional day (a day requires a month)."""

    year: int
    month: int | None = None
    day: int | None = None

    def __post_init__(self) -> None:
        valid = 1 <= self.year <= 9999 and (self.month is None or 1 <= self.month <= 12)
        if valid and self.month is None and self.day is not None:
            valid = False
        if valid and self.day is not None and self.month is not None:
            valid = 1 <= self.day <= calendar.monthrange(self.year, self.month)[1]
        if not valid:
            raise ValueError(f"invalid partial date: {self.year}-{self.month}-{self.day}")

    @property
    def precision(self) -> DatePrecision:
        if self.month is None:
            return DatePrecision.YEAR
        return DatePrecision.MONTH if self.day is None else DatePrecision.DAY

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, PartialDate):
            return NotImplemented
        return (self.year, self.month or 0, self.day or 0) < (
            other.year,
            other.month or 0,
            other.day or 0,
        )

    def iso(self) -> str:
        parts = [f"{self.year:04d}"]
        if self.month is not None:
            parts.append(f"{self.month:02d}")
            if self.day is not None:
                parts.append(f"{self.day:02d}")
        return "-".join(parts)

    __str__ = iso

    def coarsened_to(self, precision: DatePrecision) -> "PartialDate":
        """Lower the precision; asking for more than the data holds is an error."""
        order = list(DatePrecision)
        if order.index(precision) > order.index(self.precision):
            raise ValueError(f"cannot raise precision from {self.precision} to {precision}")
        if precision is DatePrecision.YEAR:
            return PartialDate(self.year)
        if precision is DatePrecision.MONTH:
            return PartialDate(self.year, self.month)
        return self


def parse_partial_date(value: str | int | float | None) -> "PartialDate | None":
    """`2023`, `2023-05`, `2023-05-14`; a null or zero value from a source gives `None`.

    A whole float (`2023.0`) is read as the integer; NaN, infinity, other floats and booleans are
    invalid. `1970-01-01` is a real date here: only an upload timestamp of 0 means "no date".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid partial date: {value!r}")
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise ValueError(f"invalid partial date: {value!r}")
        value = int(value)
    text = str(value).strip()
    if text in _ZERO_TEXTS:
        return None
    match = _PARTIAL.match(text)
    if match is None:
        raise ValueError(f"invalid partial date: {text!r}")
    year, month, day = (int(g) if g is not None else None for g in match.groups())
    try:
        return PartialDate(year or 0, month, day)
    except ValueError as exc:
        raise ValueError(f"invalid partial date: {text!r}") from exc


def from_upload_timestamp(value: object) -> datetime | None:
    """Upload date from a source (Unix seconds or milliseconds).

    Null, zero, negative, NaN, infinite, out-of-range or non-numeric values give `None`.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if text in _ZERO_TEXTS:
            return None
        value = text
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number >= _MS_THRESHOLD:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def comicinfo_date_fields(date: PartialDate) -> dict[str, int]:
    """ComicInfo `Year`/`Month`/`Day` fields, none finer than the data."""
    fields = {"Year": date.year}
    if date.month is not None:
        fields["Month"] = date.month
        if date.day is not None:
            fields["Day"] = date.day
    return fields


def opf_date(date: PartialDate) -> str:
    """OPF `dc:date`, at the precision of the data."""
    return date.iso()
