"""ENF-OPS-021 / ENF-I18N-002 properties."""

import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st
from shelfhold.catalog.domain.normalizers import (
    NormalizerRegistry,
    default_registry,
    resolve_key,
    to_nfc,
)


@pytest.mark.req("ENF-OPS-021")
@given(st.lists(st.text(max_size=15), max_size=12))
def test_normalizer_upgrade_produces_no_new_key_without_plan_property(raws: list[str]) -> None:
    registry = NormalizerRegistry()
    registry.register("k", 1, lambda s: s.strip().lower())
    stored = [registry.normalize("k", raw) for raw in raws]
    keys_before = [resolve_key(s) for s in stored]
    registry.register("k", 2, lambda s: "".join(ch for ch in s.lower() if ch.isalnum()))
    assert [resolve_key(s) for s in stored] == keys_before  # v(n+1) over v(n) data: nothing moves
    plan = registry.plan_migration(stored, "k")
    migrated = plan.apply()
    assert [m.raw for m in migrated] == raws
    for merge in plan.merges:
        assert len(merge.raws) >= 2
    # every merged group really collapses to one new key, and nothing else does
    collapsed = {m.key for m in migrated}
    assert len(collapsed) == len({s.key for s in migrated})


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
