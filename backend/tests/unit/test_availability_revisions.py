"""Open availability vocabulary and the suspect-revision rule (EF-BIB-009, EF-BIB-024)."""

import pytest

from shelfhold.catalog.domain.availability import KNOWN_AVAILABILITIES, normalize_availability
from shelfhold.catalog.domain.revisions import DEFAULT_MIN_PAGE_RATIO, suspect_reason


@pytest.mark.req("EF-BIB-009")
@pytest.mark.parametrize("value", KNOWN_AVAILABILITIES)
def test_availability_open_vocabulary_known_values_pass_through(value: str) -> None:
    got = normalize_availability(value)
    assert (got.value, got.detail) == (value, None)


@pytest.mark.req("EF-BIB-009")
@pytest.mark.parametrize("raw", ["early_access", "Geo-Blocked", "x" * 500, "  ", "", None])
def test_availability_open_vocabulary_unknown_is_locked_never_free(raw: str | None) -> None:
    got = normalize_availability(raw)
    if raw is None:
        assert got.value == "free"  # the source said nothing
        return
    assert got.value == "locked"  # a value nobody vouched for is never exported (D20)
    assert got.detail is None or len(got.detail) <= 64


@pytest.mark.req("EF-BIB-024")
def test_suspect_rule_boundaries() -> None:
    assert DEFAULT_MIN_PAGE_RATIO == 0.5
    assert suspect_reason(stored_pages=20, new_pages=9, availability="free") == "fewer_pages"
    assert suspect_reason(stored_pages=20, new_pages=10, availability="free") is None  # 50 %: ok
    assert suspect_reason(stored_pages=20, new_pages=1, availability="free") == "fewer_pages"
    assert suspect_reason(stored_pages=20, new_pages=20, availability="stubbed") == "stubbed"
    assert suspect_reason(stored_pages=None, new_pages=1, availability="free") is None
    assert suspect_reason(stored_pages=20, new_pages=None, availability="free") is None
    # an empty archive is the extreme of a truncated one, whatever the stored count
    assert suspect_reason(stored_pages=20, new_pages=0, availability="free") == "fewer_pages"
    # the threshold is adjustable
    assert suspect_reason(stored_pages=20, new_pages=15, availability="free", min_ratio=0.9)
