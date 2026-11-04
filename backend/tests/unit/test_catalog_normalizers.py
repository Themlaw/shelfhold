"""ENF-OPS-021 / ENF-I18N-002: versioned normalizers, raw values kept, NFC without bidi loss."""

import re
import unicodedata

import pytest

from shelfhold.catalog.domain.normalizers import (
    Normalized,
    NormalizerId,
    NormalizerRegistry,
    default_registry,
    resolve_key,
    to_nfc,
)

ID_FORM = re.compile(r"^[a-z_]+@[1-9][0-9]*$")
RLM, LRM, RLE, PDF = "‏", "‎", "‫", "‬"


@pytest.mark.req("ENF-OPS-021")
def test_every_identity_key_stores_normalizer_version_and_raw() -> None:
    registry = default_registry()
    assert {"series_name", "team_label", "text_version", "number", "lang"} <= set(registry.names())
    for name in registry.names():
        for raw in ("  Some  Title ", "10.5", "fr_FR", "Équipe Ünï"):
            try:
                stored = registry.normalize(name, raw)
            except ValueError:
                continue  # a normalizer may refuse an input it cannot key (e.g. a bad tag)
            assert stored.raw == raw
            assert ID_FORM.match(stored.by), stored.by
            assert stored.by == str(registry.current(name))


@pytest.mark.req("ENF-OPS-021")
def test_normalizer_id_round_trip() -> None:
    nid = NormalizerId.parse("series_name@3")
    assert (nid.name, nid.version, str(nid)) == ("series_name", 3, "series_name@3")
    for bad in ("series_name", "x@0", "x@a", "@1", "x@1@2"):
        with pytest.raises(ValueError, match="normalizer id"):
            NormalizerId.parse(bad)


@pytest.mark.req("ENF-OPS-021")
def test_registering_a_version_twice_or_out_of_order_is_refused() -> None:
    registry = NormalizerRegistry()
    registry.register("k", 1, str.lower)
    with pytest.raises(ValueError, match="already"):
        registry.register("k", 1, str.upper)
    with pytest.raises(ValueError, match="skips"):
        registry.register("k", 3, str.upper)


@pytest.mark.req("ENF-OPS-021")
def test_normalizer_upgrade_produces_no_new_key_without_plan() -> None:
    registry = NormalizerRegistry()
    registry.register("k", 1, lambda s: s.lower())
    old = [registry.normalize("k", raw) for raw in ("Berserk", "BERSERK", "Vagabond")]
    registry.register("k", 2, lambda s: s.lower().replace("e", ""))
    # nothing is recomputed on read: the stored key stays, whatever the current version says
    assert [resolve_key(s) for s in old] == ["berserk", "berserk", "vagabond"]
    plan = registry.plan_migration(old, "k")
    assert plan.target == NormalizerId("k", 2)
    assert {c.raw for c in plan.changes} == {"Berserk", "BERSERK", "Vagabond"}
    # the preview shows what merges: v2 maps both spellings of berserk to the same key
    assert [sorted(m.raws) for m in plan.merges] == [["BERSERK", "Berserk"]]
    assert plan.merges[0].new_value == "brsrk"
    migrated = plan.apply()
    assert [s.by for s in migrated] == ["k@2"] * 3
    assert all(s.raw == o.raw for s, o in zip(migrated, old, strict=True))
    assert old[0].by == "k@1"  # rows are immutable until the plan is applied


@pytest.mark.req("ENF-OPS-021")
def test_plan_ignores_rows_already_on_target_version() -> None:
    registry = NormalizerRegistry()
    registry.register("k", 1, str.lower)
    current = registry.normalize("k", "X")
    assert registry.plan_migration([current], "k").changes == ()


@pytest.mark.req("ENF-OPS-021")
def test_unknown_normalizer_or_version_is_refused() -> None:
    registry = default_registry()
    with pytest.raises(KeyError):
        registry.normalize("nope", "x")
    with pytest.raises(KeyError):
        registry.get(NormalizerId("series_name", 99))
    assert Normalized("a", "A", "series_name@1").key == "a"


@pytest.mark.req("ENF-I18N-002")
def test_unicode_nfc_and_bidi_preserved() -> None:
    assert to_nfc("école") == "école"
    assert to_nfc("école") == "école"
    arabic = f"{RLE}كتاب{PDF} {LRM}Book{RLM}"
    assert to_nfc(arabic) == arabic  # bidi controls and logical order survive
    hebrew = "ספר 2"
    assert to_nfc(hebrew) == hebrew
    assert unicodedata.is_normalized("NFC", to_nfc("Amélie 가"))


@pytest.mark.req("ENF-I18N-002", "ENF-OPS-021")
def test_series_key_is_nfc_casefolded_and_blind_to_invisible_marks() -> None:
    registry = default_registry()
    a = registry.normalize("series_name", "  Amélie   LA Série ")
    b = registry.normalize("series_name", f"{LRM}Amélie la  série")
    assert a.key == b.key == "amélie la série"
    assert a.raw == "  Amélie   LA Série "  # raw is kept verbatim
