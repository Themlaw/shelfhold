"""ComicInfo v2.0 + draft 2.1 writer (EF-EXP-011, EF-BIB-046, ENF-SEC-015, EF-BIB-022)."""

import pytest

from shelfhold.catalog.public import ContributorName, NameKind
from shelfhold.infrastructure.packaging.comicinfo import (
    ComicInfoData,
    ComicInfoPerson,
    render_comicinfo,
)
from shelfhold.infrastructure.xml import safe_parse_xml
from shelfhold.sdk.age import AgeRating
from tests.comicinfo_xsd import kavita_schema, schema, strip_kavita_elements


def person(name: str, *, org: bool = False, kind: NameKind = NameKind.DISPLAY) -> ComicInfoPerson:
    return ComicInfoPerson((ContributorName(name, "fr", "Latn", kind),), is_org=org)


def text_of(xml: bytes, tag: str) -> str | None:
    node = safe_parse_xml(xml).find(tag)
    return None if node is None else (node.text or "")


FULL = ComicInfoData(
    series="Dr Stone",
    title="Chapitre 311",
    number="311",
    volume=35,
    series_sort="Stone, Dr",
    localized_series="ドクターストーン",
    summary="Un résumé.",
    year=2019,
    month=3,
    day=8,
    people={"Writer": (person("Riichiro Inagaki"),), "Penciller": (person("Boichi"),)},
    team_label="Équipe Aurore",
    publisher="Éditeur",
    genres=("Aventure", "Science-fiction"),
    language_iso="fr",
    age=AgeRating(13),
    right_to_left=True,
    page_count=18,
)


@pytest.mark.req("EF-EXP-011", "ENF-QUAL-004")
def test_comicinfo_validates_pinned_xsd() -> None:
    rendered = render_comicinfo(FULL)
    xml = rendered.xml.decode("utf-8")
    # draft 2.1 as pinned (Translator is a 2.1 field), without the two Kavita-only elements
    schema("v2.1-draft").validate(strip_kavita_elements(xml))
    # and the whole document against the draft completed with those two elements
    kavita_schema().validate(xml)
    # a document without any 2.1 field is also valid v2.0
    plain = render_comicinfo(ComicInfoData(series="Dr Stone", number="1", age=AgeRating(0)))
    schema("v2.0").validate(plain.xml.decode("utf-8"))
    assert render_comicinfo(FULL).xml == rendered.xml  # deterministic bytes


@pytest.mark.req("EF-EXP-011")
def test_sortname_and_localized_series_exported() -> None:
    xml = render_comicinfo(FULL).xml
    assert text_of(xml, "SeriesSort") == "Stone, Dr"
    assert text_of(xml, "LocalizedSeries") == "ドクターストーン"
    empty = render_comicinfo(ComicInfoData(series="Dr Stone")).xml
    assert text_of(empty, "SeriesSort") is None and text_of(empty, "LocalizedSeries") is None


@pytest.mark.req("ENF-SEC-015")
def test_comicinfo_summary_plain_or_sanitized() -> None:
    hostile = '<script>alert(1)</script><b onclick="x()">Gras</b> <a href="javascript:x()">lien</a>'
    plain = render_comicinfo(ComicInfoData(series="S", summary=hostile)).xml
    assert text_of(plain, "Summary") == "Gras lien"
    html = render_comicinfo(ComicInfoData(series="S", summary=hostile, summary_html=True)).xml
    summary = text_of(html, "Summary") or ""
    assert "<b>Gras</b>" in summary
    for bad in ("script", "onclick", "javascript:"):
        assert bad not in summary.lower()
    # the raw text never appears unescaped in the XML itself
    assert b"<script" not in plain and b"<script" not in html
    kavita_schema().validate(html.decode("utf-8"))


@pytest.mark.req("EF-BIB-046")
def test_comicinfo_writer_never_contains_inverted_name() -> None:
    data = ComicInfoData(
        series="S",
        people={
            "Writer": (person("Inagaki, Riichiro"), person("Boichi")),
            "Editor": (person("Éditeur, Ltd.", org=True),),
        },
    )
    rendered = render_comicinfo(data)
    assert text_of(rendered.xml, "Writer") == "Riichiro Inagaki, Boichi"
    assert text_of(rendered.xml, "Editor") == "Éditeur Ltd."  # comma replaced for an organisation
    assert any("comma" in w for w in rendered.warnings)  # journaled
    # a sort name is never a person field
    with pytest.raises(ValueError, match="sort"):
        render_comicinfo(
            ComicInfoData(
                series="S", people={"Writer": (person("Inagaki, R", kind=NameKind.SORT),)}
            )
        )


@pytest.mark.req("EF-EXP-011", "EF-EXP-063")
def test_comicinfo_translator_is_team_web_empty_by_default() -> None:
    default = render_comicinfo(ComicInfoData(series="S")).xml
    assert text_of(default, "Translator") is None
    assert text_of(default, "Web") is None
    with_team = render_comicinfo(ComicInfoData(series="S", team_label="Équipe Aurore")).xml
    assert text_of(with_team, "Translator") == "Équipe Aurore"
    assert text_of(with_team, "Web") is None  # no source reference unless explicitly given


@pytest.mark.req("EF-EXP-063")
def test_exported_file_carries_no_shelfhold_identity() -> None:
    xml = render_comicinfo(FULL).xml
    lowered = xml.lower()
    assert b"shelfhold" not in lowered
    assert b"rayonnage" not in lowered  # the former name of the tool
    for tag in ("Notes", "ScanInformation", "Web", "GTIN", "Tags"):
        assert text_of(xml, tag) is None
    assert "Identifiant" not in xml.decode()


@pytest.mark.req("EF-EXP-011")
def test_text_is_xml_escaped_and_control_characters_removed() -> None:
    rendered = render_comicinfo(ComicInfoData(series="A & B <c> \x00\x0b d", title="\ud800x"))
    assert text_of(rendered.xml, "Series") == "A & B <c>  d"
    kavita_schema().validate(rendered.xml.decode("utf-8"))


@pytest.mark.req("EF-BIB-046")
def test_commas_replaced_in_translator_publisher_and_genres() -> None:
    rendered = render_comicinfo(
        ComicInfoData(
            series="S",
            team_label="Équipe, Aurore",
            publisher="Éditeur, Ltd.",
            genres=("Aventure, Action", "Drame"),
        )
    )
    assert text_of(rendered.xml, "Translator") == "Équipe Aurore"
    assert text_of(rendered.xml, "Publisher") == "Éditeur Ltd."
    assert text_of(rendered.xml, "Genre") == "Aventure Action, Drame"  # the only commas separate
    assert len(rendered.warnings) == 3
