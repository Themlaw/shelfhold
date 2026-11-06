"""The date of a release as the database keeps it (EF-BIB-047).

What a source says about when a release came out is stored as a partial ISO 8601 date with its
precision: never finer than the source knew, and a null or zero upload time is no date at all.
"""

from datetime import UTC, datetime

from shelfhold.catalog.domain.dates import parse_partial_date

__all__ = ["period_start", "stored_date"]


def period_start(text: str) -> datetime:
    """The first instant (UTC) of the period a partial date names (ValueError if it is none)."""
    date = parse_partial_date(text)
    if date is None:
        raise ValueError(f"not a date: {text!r}")
    return datetime(date.year, date.month or 1, date.day or 1, tzinfo=UTC)


def stored_date(
    published_on: str | None, precision: str | None, published_at: datetime | None
) -> tuple[str, str] | None:
    """`(published_on, date_precision)` to store, or `None`.

    The date the source gave wins, but only when well formed and when the precision it announces
    is the one it has; otherwise the UTC day of the upload time, when there is one.
    """
    if published_on is not None:
        try:
            date = parse_partial_date(published_on)
        except ValueError:
            return None
        if date is None or date.precision.value != precision:
            return None
        return date.iso(), date.precision.value
    if published_at is None or published_at.tzinfo is None or published_at.timestamp() <= 0:
        return None
    return published_at.astimezone(UTC).date().isoformat(), "day"
