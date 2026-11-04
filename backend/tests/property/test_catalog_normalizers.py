"""ENF-OPS-021 / ENF-I18N-002 properties."""

import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shelfhold.catalog.domain.normalizers import (
    default_registry,
    resolve_key,
    to_nfc,
)


def _alnum_only(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


@pytest.mark.req("ENF-OPS-021")
@given(st.lists(st.text(max_size=15), max_size=12))
def test_normalizer_upgrade_produces_no_new_key_without_plan_property(raws: list[str]) -> None:
    registry = default_registry()
    v1 = registry.current("series_name")
    stored = [registry.normalize("series_name", raw) for raw in raws]
    expected_v1 = [registry.get(v1)(raw) for raw in raws]
    v2 = registry.register("series_name", 2, _alnum_only)
    # New rows get v2 keys; the rows stored under v1 keep exactly the v1 key, unrecomputed.
    assert [resolve_key(s) for s in stored] == expected_v1
    assert all(s.by == str(v1) for s in stored)
    fresh = [registry.normalize("series_name", raw) for raw in raws]
    assert [f.key for f in fresh] == [_alnum_only(raw) for raw in raws]
    # Only an explicit plan produces v2 keys, and it leaves the stored rows untouched.
    plan = registry.plan_migration(stored, "series_name")
    migrated = plan.apply()
    assert [m.key for m in migrated] == [f.key for f in fresh]
    assert [m.by for m in migrated] == [str(v2)] * len(raws)
    assert [resolve_key(s) for s in stored] == expected_v1
    # Every merge preview is a real collision of two different raw values on one new key.
    for merge in plan.merges:
        assert len(merge.raws) >= 2
        assert {_alnum_only(raw) for raw in merge.raws} == {merge.new_value}
    new_keys = [m.key for m in migrated]
    for key in {
        k
        for k in new_keys
        if new_keys.count(k) > 1
        and len({r for r, m in zip(raws, new_keys, strict=True) if m == k}) > 1
    }:
        assert key in {m.new_value for m in plan.merges}


@pytest.mark.req("ENF-I18N-002")
@given(st.text(max_size=40))
def test_to_nfc_is_idempotent_and_normalized(text: str) -> None:
    once = to_nfc(text)
    assert unicodedata.is_normalized("NFC", once)
    assert to_nfc(once) == once


@pytest.mark.req("ENF-I18N-002")
@given(st.text(alphabet=st.characters(categories=["Lu", "Ll", "Lo", "Mn"]), max_size=30))
def test_series_key_is_stable_under_nfd_nfc(text: str) -> None:
    registry = default_registry()
    nfd = unicodedata.normalize("NFD", text)
    assert registry.normalize("series_name", nfd).key == registry.normalize("series_name", text).key
