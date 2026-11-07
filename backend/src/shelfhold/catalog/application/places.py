"""The place a release takes in its Edition, as a stable text (EF-BIB-034)."""

from shelfhold.catalog.domain.numbering import NumberError, parse_number
from shelfhold.catalog.domain.placement import place_of

__all__ = ["place_key"]


def place_key(raw_number: str | None, source_order: int = 0) -> str:
    """A stable text for the place a release takes in its Edition (EF-BIB-034): two releases with
    the same key are offers for the same Unit. An unnumbered release is placed by its order."""
    try:
        number = parse_number(raw_number, source_order=source_order)
    except NumberError:  # unreadable: unnumbered, like the ingest
        number = parse_number(None, source_order=source_order)
    place = place_of(number)
    if place.unnumbered:
        return f"unnumbered:{source_order}"
    return f"{place.track}|{place.segment}|{place.number_norm}|{place.part}"
