"""EF-BIB-028: numbering offset between teams (L21)."""

import pytest

from shelfhold.catalog.domain.team_offset import (
    check,
    check_offset,
    consistent,
    is_consistent,
    offset_ok,
)


@pytest.mark.req("EF-BIB-028")
def test_offset_title_mismatch_suspends() -> None:
    assert not check(normalized_title="Foo", other_normalized_title="Bar").ok
    assert check(normalized_title="Foo", other_normalized_title="Foo").ok
    assert offset_ok(normalized_title="Foo", other_normalized_title="Foo")
    assert not offset_ok(normalized_title="Foo", other_normalized_title="Bar")


@pytest.mark.req("EF-BIB-028")
def test_offset_page_count_mismatch_suspends() -> None:
    assert not check(page_count=10, other_page_count=20).ok
    assert check(page_count=10, other_page_count=10).ok


@pytest.mark.req("EF-BIB-028")
def test_aliases_agree() -> None:
    assert check is check_offset is consistent
    assert is_consistent(normalized_title="Foo", other_normalized_title="Foo")
