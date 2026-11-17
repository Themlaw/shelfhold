"""M4 L30 (EF-USR-019): the comparison of titles folds Unicode, it does not reduce to ASCII."""

import pytest

from shelfhold.catalog.domain.fold import fold_title

pytestmark = pytest.mark.req("EF-USR-019")


def test_title_match_folds_unicode_not_ascii_only_function() -> None:
    assert fold_title("Élise") == fold_title("ELISE") == fold_title("élise") == "elise"
    assert fold_title("\uff45\uff4c\uff49\uff53\uff45") == "elise"  # NFKC: full width letters
    assert fold_title("ﬁn") == fold_title("FIN")  # NFKC: a ligature
    assert fold_title("Straße") == fold_title("STRASSE")  # case folding, not lower()
    assert fold_title("  Elise \t Tome   1 ") == "elise tome 1"
    assert fold_title("élise") == "elise"  # a decomposed accent folds like a composed one


def test_no_ascii_reduction_distinct_scripts_stay_distinct() -> None:
    # a reduction to ASCII would make all of these the empty string, and so one title
    assert fold_title("日本語") == "日本語"
    assert fold_title("日本語") != fold_title("中文")
    assert fold_title("Мир") != fold_title("Mup")  # Cyrillic is not transliterated
    assert fold_title("Ёлка") == fold_title("ёлка")
    assert fold_title("") == ""
