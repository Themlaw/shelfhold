"""The suspect-revision rule (EF-BIB-024).

A new version of a chapter that counts less than half of the pages of the stored one, or that the
source now gives as `stubbed`, is not a better version: it is quarantined and the stored one stays
exported. The ratio is a setting; the rule only reads numbers.
"""

from typing import Final

DEFAULT_MIN_PAGE_RATIO: Final = 0.5
FEWER_PAGES: Final = "fewer_pages"
STUBBED: Final = "stubbed"
SUSPECT_REASONS: Final = (FEWER_PAGES, STUBBED)


def suspect_reason(
    *,
    stored_pages: int | None,
    new_pages: int | None,
    availability: str,
    min_ratio: float = DEFAULT_MIN_PAGE_RATIO,
) -> str | None:
    """Why the new version is suspect, or None. A count that is unknown on either side says
    nothing about the pages (the guard does not invent a verdict)."""
    if availability == STUBBED:
        return STUBBED
    if stored_pages is None or new_pages is None or stored_pages <= 0:
        return None
    if new_pages < stored_pages * min_ratio:
        return FEWER_PAGES
    return None
