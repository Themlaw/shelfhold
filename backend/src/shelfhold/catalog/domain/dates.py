"""Dates partielles (EF-BIB-047) : ISO 8601 partiel, jamais plus précis que la donnée."""

import calendar
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
_ZERO_TEXTS = frozenset({"", "0", "0000", "0000-00", "0000-00-00", "1970-01-01"})
_MS_THRESHOLD = 100_000_000_000  # above this a Unix timestamp is in milliseconds (year 5138 in s)


@total_ordering
@dataclass(frozen=True)
class PartialDate:
    """Année, mois facultatif, jour facultatif (un jour exige un mois)."""

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
        """Réduit la précision ; en demander plus que la donnée est une erreur."""
        order = list(DatePrecision)
        if order.index(precision) > order.index(self.precision):
            raise ValueError(f"cannot raise precision from {self.precision} to {precision}")
        if precision is DatePrecision.YEAR:
            return PartialDate(self.year)
        if precision is DatePrecision.MONTH:
            return PartialDate(self.year, self.month)
        return self


def parse_partial_date(value: str | int | float | None) -> "PartialDate | None":
    """`2023`, `2023-05`, `2023-05-14` ; une valeur nulle ou à zéro de la source donne `None`."""
    if value is None:
        return None
    text = str(value).strip()
    if text in _ZERO_TEXTS or text == "0.0":
        return None
    match = _PARTIAL.match(text)
    if match is None:
        raise ValueError(f"invalid partial date: {text!r}")
    year, month, day = (int(g) if g is not None else None for g in match.groups())
    try:
        return PartialDate(year or 0, month, day)
    except ValueError as exc:
        raise ValueError(f"invalid partial date: {text!r}") from exc


def from_upload_timestamp(value: str | int | float | None) -> datetime | None:
    """Date de mise en ligne d'une source (secondes ou millisecondes Unix) ; 0 ou nul → `None`."""
    if value is None or (isinstance(value, str) and value.strip() in _ZERO_TEXTS):
        return None
    number = float(value)
    if number <= 0:
        return None
    if number >= _MS_THRESHOLD:
        number /= 1000
    return datetime.fromtimestamp(number, tz=UTC)


def comicinfo_date_fields(date: PartialDate) -> dict[str, int]:
    """Champs `Year`/`Month`/`Day` de ComicInfo, sans aucun champ plus fin que la donnée."""
    fields = {"Year": date.year}
    if date.month is not None:
        fields["Month"] = date.month
        if date.day is not None:
            fields["Day"] = date.day
    return fields


def opf_date(date: PartialDate) -> str:
    """`dc:date` de l'OPF, à la précision de la donnée."""
    return date.iso()
