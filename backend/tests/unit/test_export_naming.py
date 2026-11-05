"""Kavita naming rules (EF-EXP-004, EF-EXP-045, EF-EXP-013, EF-EXP-061) against the simulated
`plugin/parse` (tests/fakes/kavita/plugin_parse.py)."""

import json

import pytest

from shelfhold.export.domain.naming import (
    DEFAULT_TEMPLATE,
    DEFAULT_TEMPLATE_REF,
    FrozenAttrs,
    NamingTemplate,
    check_incoming,
    copy_template_into_binding,
    export_path,
    find_number_collisions,
    is_chapter_zero,
    manifest_entry,
    series_cover_source,
    volume_deduced,
)
from tests.fakes.kavita.plugin_parse import LOOSE_LEAF, SPECIAL_VOLUME, simulate_parse

UPSTREAM_TITLES = [
    "Chapter 311 - {S1 END}",
    "Chapter 12 [S1 END]",
    "Ch.40 - S5 Start",
    "Vol.3 Chapter 9",
    "Season 2 - Finale (Vol 4)",
]


def chapter(number: str, series: str = "Dr Stone", **kw: object) -> FrozenAttrs:
    return FrozenAttrs(series=series, track="main", number_norm=number, **kw)  # type: ignore[arg-type]


@pytest.mark.req("EF-EXP-045")
def test_simulated_parser_reads_season_markers_as_volumes() -> None:
    """The replica must reproduce the trap the rule exists for."""
    assert volume_deduced(simulate_parse("Dr Stone - Ch.311 - {S1 END}.cbz"))
    assert volume_deduced(simulate_parse("Dr Stone - Vol.3 Ch.9.cbz"))
    assert not volume_deduced(simulate_parse("Dr Stone - Ch.311.cbz"))


@pytest.mark.req("EF-EXP-045")
@pytest.mark.parametrize("title", UPSTREAM_TITLES)
@pytest.mark.parametrize("number", ["1", "311", "10.5", "0.5"])
def test_filename_never_yields_volume_via_plugin_parse(title: str, number: str) -> None:
    """The upstream title never reaches the file name: it is not even an input of the path."""
    path = export_path(chapter(number))
    assert title not in path.filename
    parsed = simulate_parse(path.filename)
    assert not volume_deduced(parsed)
    assert parsed.min_chapter == float(number)
    assert parsed.series_name == "Dr Stone"


@pytest.mark.req("EF-EXP-045")
def test_no_two_files_same_number_in_series_dir() -> None:
    places = [chapter(str(n)) for n in (1, 2, 10, 10.5, 11)]
    names = [export_path(p).filename for p in places]
    assert find_number_collisions(names, simulate_parse) == []
    # a second part of a chapter shares the parsed number: reported, never silently written
    names.append(export_path(chapter("10", part=2)).filename)
    collisions = find_number_collisions(names, simulate_parse)
    assert len(collisions) == 1
    assert {
        export_path(chapter("10")).filename,
        export_path(chapter("10", part=2)).filename,
    } == set(collisions[0].names)


@pytest.mark.req("EF-EXP-045")
def test_adopted_file_with_same_number_wins_by_default() -> None:
    """An adopted Ch.112 becomes the exported Variant: nothing written, nothing replaced."""
    existing = ["Dr Stone - Ch.111.cbz", "Dr Stone - Ch.112 [Team].cbz"]
    decision = check_incoming(export_path(chapter("112")).filename, existing, simulate_parse)
    assert decision is not None
    assert decision.action == "attach_adopted"
    assert decision.existing == "Dr Stone - Ch.112 [Team].cbz"
    assert check_incoming(export_path(chapter("113")).filename, existing, simulate_parse) is None


@pytest.mark.req("EF-EXP-045")
def test_series_name_normalised_like_kavita_for_collisions() -> None:
    from shelfhold.export.domain.naming import normalize_series_name

    assert normalize_series_name("Dr. STONE") == normalize_series_name("dr stone")
    assert normalize_series_name("Frieren: Beyond Journey\u2019s End") == normalize_series_name(
        "Frieren Beyond Journey's End"
    )
    assert normalize_series_name("Re:Zero") != normalize_series_name("Re Zero 2")


@pytest.mark.req("EF-EXP-045")
def test_chapter_zero_special_cover_from_chapter_one() -> None:
    zero, one, two = chapter("0"), chapter("1"), chapter("2")
    assert is_chapter_zero(zero) and not is_chapter_zero(one)
    path = export_path(zero)
    assert path.directory[-1] == "Specials"
    parsed = simulate_parse(path.filename)
    assert parsed.min_volume == SPECIAL_VOLUME
    # the series cover is chosen from chapter 1, never from the special 0
    assert series_cover_source([zero, two, one]) == one
    assert series_cover_source([zero]) is None
    assert series_cover_source([zero, chapter("0.5")]) is None


@pytest.mark.req("EF-EXP-004")
def test_template_copied_into_binding_with_origin() -> None:
    binding = copy_template_into_binding(DEFAULT_TEMPLATE)
    assert binding.origin == f"default@{DEFAULT_TEMPLATE.version}"
    assert NamingTemplate.loads(binding.text) == DEFAULT_TEMPLATE
    # a new default is only proposed: the binding keeps its own copy
    newer = NamingTemplate(
        template_id=DEFAULT_TEMPLATE.template_id,
        version=DEFAULT_TEMPLATE.version + 1,
        chapter="{series} Chapter {number}",
    )
    assert NamingTemplate.loads(binding.text) != newer
    assert copy_template_into_binding(newer).origin == f"default@{newer.version}"
    assert json.loads(binding.text)["template_id"] == DEFAULT_TEMPLATE.template_id


@pytest.mark.req("EF-EXP-004")
def test_manifest_records_template_id_and_version_per_file() -> None:
    items = [chapter("1"), chapter("2"), FrozenAttrs("Dr Stone", "main", "0")]
    entries = [manifest_entry(i, export_path(i)) for i in items]
    assert {e.template_ref for e in entries} == {DEFAULT_TEMPLATE_REF}
    assert f"{DEFAULT_TEMPLATE.template_id}@{DEFAULT_TEMPLATE.version}" == DEFAULT_TEMPLATE_REF
    assert [e.rel_path for e in entries] == [export_path(i).rel_path for i in items]
    # a file made by an older template keeps naming itself with that template
    old = NamingTemplate("kavita-manga", 0, chapter="{series} - Chapter {number}")
    frozen = chapter("5", template_ref=old.ref)
    assert export_path(frozen, old).filename == "Dr Stone - Chapter 5.cbz"
    assert manifest_entry(frozen, export_path(frozen, old)).template_ref == "kavita-manga@0"
    with pytest.raises(ValueError, match="template"):
        export_path(frozen, DEFAULT_TEMPLATE)


@pytest.mark.req("EF-EXP-013")
def test_title_with_reserved_characters_gives_a_valid_path() -> None:
    path = export_path(chapter("3", series='Who? "Am" I: a/b\\c*<d>|'))
    for part in (*path.directory, path.filename):
        assert not set('/\\:*?"<>|') & set(part)
        assert part == part.strip() and not part.endswith(".")
    assert path.filename.endswith(" - Ch.3.cbz")


def test_loose_leaf_constant_is_kavitas() -> None:
    assert LOOSE_LEAF == -100000.0
