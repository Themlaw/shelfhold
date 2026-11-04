"""EF-BIB-002 golden: fixed parser outcomes (track, segment, norm, part) for raw numbers."""

from decimal import Decimal

import pytest

from shelfhold.catalog.domain.numbering import Track, parse_number

# raw -> (track, segment, norm, part)
GOLDEN: dict[str, tuple[str, str, str, int]] = {
    "10": ("main", "", "10", 1),
    "10.5": ("main", "", "10.5", 1),
    "10,5": ("main", "", "10.5", 1),
    "10\u066b5": ("main", "", "10.5", 1),  # Arabic decimal separator
    "1,000": ("main", "", "1", 1),  # the comma is always a decimal separator
    "1.000": ("main", "", "1", 1),
    "Vol.2 Ch.10": ("main", "", "10", 1),
    "Vol. 2 Chapter 11": ("main", "", "11", 1),
    "Volume 2 Episode 12": ("main", "", "12", 1),
    "Vol.2 #13": ("main", "", "13", 1),
    "Vol.2": ("main", "", "2", 1),
    "S2 Ch.40": ("main", "S2", "40", 1),
    "Season 3 Chapter 4": ("main", "S3", "4", 1),
    "S2E5": ("main", "S2", "5", 1),
    "10 part 2": ("main", "", "10", 2),
    "Annual 1": ("annual", "", "1", 1),
    "Extra": ("extra", "", "", 1),
    "One-shot": ("oneshot", "", "", 1),
    "nothing": ("unnumbered", "", "", 1),
    "": ("unnumbered", "", "", 1),
}


@pytest.mark.req("EF-BIB-002")
@pytest.mark.parametrize("raw", sorted(GOLDEN))
def test_parser_golden(raw: str) -> None:
    parsed = parse_number(raw)
    assert (parsed.track, parsed.segment, parsed.norm, parsed.part) == (
        Track(GOLDEN[raw][0]),
        *GOLDEN[raw][1:],
    )
    assert parsed.sort == (Decimal(parsed.norm) if parsed.norm else Decimal(0))
