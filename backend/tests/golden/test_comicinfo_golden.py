"""ComicInfo goldens (ENF-QUAL-004) and the frozen age table (EF-EXP-011, B4, L5)."""

import os
from pathlib import Path

import pytest

from shelfhold.infrastructure.packaging.comicinfo import ComicInfoData, render_comicinfo
from shelfhold.sdk.age import AgeRating
from tests.comicinfo_xsd import kavita_schema

GOLDEN = Path(__file__).parent / "comicinfo"

# The seven levels of the scale (12+ included) and "unrated", with the exact AgeRating value.
AGE_TABLE: list[tuple[str, AgeRating | None, str | None]] = [
    ("all", AgeRating(0), "Everyone"),
    ("10", AgeRating(10), "Everyone 10+"),
    ("12", AgeRating(12), "Teen"),
    ("13", AgeRating(13), "Teen"),
    ("16", AgeRating(16), "Mature 17+"),
    ("18", AgeRating(18), "Adults Only 18+"),
    ("x", AgeRating(18, x=True), "X18+"),
    ("unrated", None, None),
]


@pytest.mark.req("EF-EXP-011", "ENF-QUAL-004")
@pytest.mark.parametrize(("label", "rating", "expected"), AGE_TABLE, ids=[a[0] for a in AGE_TABLE])
def test_age_rating_mapping_table_golden(
    label: str, rating: AgeRating | None, expected: str | None
) -> None:
    xml = render_comicinfo(ComicInfoData(series="Dr Stone", number="1", age=rating)).xml
    text = xml.decode("utf-8")
    kavita_schema().validate(text)
    if expected is None:
        assert "<AgeRating>" not in text  # unrated: no value at all (and only an adult shelf)
    else:
        assert f"<AgeRating>{expected}</AgeRating>" in text
    target = GOLDEN / f"age_{label}.xml"
    if os.environ.get("SHELFHOLD_UPDATE_GOLDEN") == "1":
        target.write_bytes(xml)
    assert target.read_bytes() == xml


@pytest.mark.req("EF-EXP-011", "ENF-QUAL-004")
def test_full_comicinfo_golden() -> None:
    from shelfhold.catalog.public import ContributorName, NameKind
    from shelfhold.infrastructure.packaging.comicinfo import ComicInfoPerson

    def who(name: str) -> ComicInfoPerson:
        return ComicInfoPerson((ContributorName(name, "fr", "Latn", NameKind.DISPLAY),))

    data = ComicInfoData(
        series="Dr Stone", title="Chapitre 311", number="311", volume=35,
        series_sort="Stone, Dr", localized_series="ドクターストーン", summary="Un <b>résumé</b>.",
        year=2019, month=3, day=8,
        people={"Writer": (who("Inagaki, Riichiro"),), "Penciller": (who("Boichi"),)},
        team_label="Équipe Exemple", publisher="Éditeur", genres=("Aventure", "Science-fiction"),
        language_iso="fr", age=AgeRating(13), right_to_left=True, page_count=18,
    )  # fmt: skip
    xml = render_comicinfo(data).xml
    kavita_schema().validate(xml.decode("utf-8"))
    target = GOLDEN / "full.xml"
    if os.environ.get("SHELFHOLD_UPDATE_GOLDEN") == "1":
        target.write_bytes(xml)
    assert target.read_bytes() == xml
