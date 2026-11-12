"""EF-EXP-005: the naming preview of a changed template, read by `plugin/parse` (the replica of
v0.9.1.4), the collisions it must flag, and the "not verified" mark. Attack tests first."""

from dataclasses import replace

import pytest

from shelfhold.export.application.naming_preview import (
    NamingItem,
    preview_names,
)
from shelfhold.export.domain.kavita_compat import parser_replica_covers
from shelfhold.export.domain.naming import (
    DEFAULT_TEMPLATE,
    MAX_COMPONENT_BYTES,
    FrozenAttrs,
    NamingTemplate,
)
from tests.fakes.kavita.plugin_parse import simulate_parse

TEMPLATE = replace(DEFAULT_TEMPLATE, version=2, chapter="{series} Ch {number}")


def item(key: str, number: str = "1", series: str = "Dr Stone", **kw: object) -> NamingItem:
    frozen = FrozenAttrs(
        series=series, track="main", number_norm=number, template_ref=TEMPLATE.ref, **kw  # type: ignore[arg-type]
    )
    return NamingItem(key, frozen, ".cbz")


def flags_of(preview: object, key: str) -> tuple[str, ...]:
    return next(e.flags for e in preview.entries if e.key == key)  # type: ignore[attr-defined]


@pytest.mark.req("EF-EXP-005")
def test_naming_preview_via_plugin_parse() -> None:
    items = [item(str(n), str(n)) for n in (1, 2, 10)]
    preview = preview_names(items, TEMPLATE, parse=simulate_parse, verified=True)
    assert preview.verified
    assert [e.status for e in preview.entries] == ["confirmed"] * 3
    assert [e.rel_path for e in preview.entries][0] == "Dr Stone/Dr Stone Ch 1.cbz"
    assert preview.collisions == ()


@pytest.mark.req("EF-EXP-005")
def test_naming_preview_flags_a_name_kavita_reads_as_another_number() -> None:
    """A template that Kavita reads wrongly (a number glued to a volume marker) is signalled."""
    bad = replace(TEMPLATE, chapter="{series} S{number}")  # `S<n>` is a volume in 0.9.1.4
    entries = preview_names(
        [item("a", "5", template_ref=bad.ref)], bad, parse=simulate_parse, verified=True
    ).entries
    assert entries[0].status == "flagged"
    assert entries[0].flags


@pytest.mark.req("EF-EXP-005")
def test_naming_preview_unverified_when_kavita_down() -> None:
    preview = preview_names([item("a")], TEMPLATE, parse=None, verified=False)
    assert not preview.verified
    assert [e.status for e in preview.entries] == ["unverified"]
    assert preview.entries[0].rel_path == "Dr Stone/Dr Stone Ch 1.cbz"  # the names still show


@pytest.mark.req("EF-EXP-005")
def test_naming_preview_unverified_when_verified_is_withheld_even_with_a_parser() -> None:
    preview = preview_names([item("a")], TEMPLATE, parse=simulate_parse, verified=False)
    assert [e.status for e in preview.entries] == ["unverified"]


@pytest.mark.req("EF-EXP-005")
@pytest.mark.parametrize(
    ("version", "covered"),
    [("0.9.1.4", True), ("v0.9.1.5", True), ("0.9.1", True), ("0.9.0.9", False),
     ("0.10.0", False), ("1.0.0", False), ("", False), (None, False), ("nightly", False)],
)  # fmt: skip
def test_the_mark_follows_the_versions_the_replica_covers(
    version: str | None, covered: bool
) -> None:
    assert parser_replica_covers(version) is covered


@pytest.mark.req("EF-EXP-005")
def test_number_collision_flagged_in_preview() -> None:
    """Chapter 10 and its second part are the same number for Kavita: both are flagged."""
    items = [item("a", "10"), item("b", "10", part=2), item("c", "11")]
    preview = preview_names(items, TEMPLATE, parse=simulate_parse, verified=True)
    assert "number_collision" in flags_of(preview, "a")
    assert "number_collision" in flags_of(preview, "b")
    assert flags_of(preview, "c") == ()
    assert len(preview.collisions) == 1
    assert preview.collisions[0].kind == "number"


@pytest.mark.req("EF-EXP-005")
def test_attack_two_units_with_the_same_path() -> None:
    preview = preview_names(
        [item("a", "3"), item("b", "3")], TEMPLATE, parse=simulate_parse, verified=True
    )
    assert "path_collision" in flags_of(preview, "a")
    assert "path_collision" in flags_of(preview, "b")
    assert all(e.status == "flagged" for e in preview.entries)


@pytest.mark.req("EF-EXP-005")
def test_attack_case_differs_on_a_case_insensitive_filesystem() -> None:
    items = [item("a", series="Dr Stone"), item("b", series="dr stone")]
    folded = preview_names(items, TEMPLATE, parse=simulate_parse, verified=True)
    assert "path_collision" in flags_of(folded, "a") + flags_of(folded, "b")
    exact = preview_names(
        items, TEMPLATE, parse=simulate_parse, verified=True, case_insensitive=False
    )
    assert "path_collision" not in flags_of(exact, "a") + flags_of(exact, "b")


@pytest.mark.req("EF-EXP-005")
def test_attack_nfc_and_nfd_spellings_are_the_same_path_everywhere() -> None:
    nfc, nfd = "Café", "Café"
    assert nfc != nfd
    for insensitive in (True, False):
        preview = preview_names(
            [item("a", series=nfc), item("b", series=nfd)],
            TEMPLATE, parse=simulate_parse, verified=True, case_insensitive=insensitive,
        )  # fmt: skip
        assert "path_collision" in flags_of(preview, "a")
        assert "path_collision" in flags_of(preview, "b")


@pytest.mark.req("EF-EXP-005")
def test_attack_a_file_already_there_with_another_case_and_form() -> None:
    preview = preview_names(
        [item("a", series="Café")], TEMPLATE, parse=simulate_parse, verified=True,
        existing=["CAFÉ/CAFÉ Ch 1.cbz"],
    )  # fmt: skip
    assert "path_taken" in flags_of(preview, "a")


@pytest.mark.req("EF-EXP-005")
@pytest.mark.parametrize("series", [".shelfhold", ".SHELFHOLD", " .Shelfhold "])
def test_attack_series_named_like_the_reserved_folder(series: str) -> None:
    preview = preview_names([item("a", series=series)], TEMPLATE, parse=None, verified=False)
    assert "reserved_name" in flags_of(preview, "a")


@pytest.mark.req("EF-EXP-005")
def test_attack_template_whose_folder_is_the_reserved_folder() -> None:
    sneaky = replace(TEMPLATE, special_dir=".shelfhold")
    special = FrozenAttrs("Dr Stone", "special", "1", template_ref=sneaky.ref)
    preview = preview_names(
        [NamingItem("a", special, ".cbz")], sneaky, parse=None, verified=False
    )
    assert "reserved_name" in preview.entries[0].flags


@pytest.mark.req("EF-EXP-005")
def test_attack_longest_names_never_exceed_a_component_and_are_flagged() -> None:
    long_series = "\U0001f600" * 120  # 480 bytes
    preview = preview_names(
        [item("a", series=long_series)], TEMPLATE, parse=None, verified=False
    )
    entry = preview.entries[0]
    assert "truncated" in entry.flags
    assert all(len(part.encode()) <= MAX_COMPONENT_BYTES for part in entry.rel_path.split("/"))


@pytest.mark.req("EF-EXP-005")
def test_attack_two_long_series_cut_to_the_same_name_collide() -> None:
    base = "a" * 300
    preview = preview_names(
        [item("a", series=base + "1"), item("b", series=base + "2")],
        TEMPLATE, parse=simulate_parse, verified=True,
    )  # fmt: skip
    assert "path_collision" in flags_of(preview, "a")
    assert "path_collision" in flags_of(preview, "b")


@pytest.mark.req("EF-EXP-005")
def test_a_clean_preview_has_no_flag_at_all() -> None:
    preview = preview_names(
        [item("a", "1"), item("b", "2", series="Other")],
        TEMPLATE, parse=simulate_parse, verified=True,
    )  # fmt: skip
    assert all(e.flags == () for e in preview.entries)


@pytest.mark.req("EF-EXP-005")
def test_the_preview_of_the_manifest_entry_carries_the_template_that_names_it() -> None:
    preview = preview_names([item("a")], TEMPLATE, parse=None, verified=False)
    assert preview.entries[0].template_ref == TEMPLATE.ref == NamingTemplate(
        template_id="kavita-manga", version=2
    ).ref
