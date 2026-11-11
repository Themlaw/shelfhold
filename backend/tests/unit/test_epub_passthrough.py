"""EPUB passthrough (EF-EXP-033, ENF-I18N-003): a targeted rewrite of the OPF, nothing else."""

import ast
import io
import os
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element

import pytest

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.packaging.epub import OpfEdit, repackage_epub, rewrite_opf
from shelfhold.infrastructure.xml import safe_parse_xml
from tests.epub_env import RIGHTS_XML, WATERMARK, epub, opf

GOLDEN = Path(__file__).parents[1] / "golden" / "epub"
SRC = Path(__file__).parents[2] / "src" / "shelfhold"
EDIT = OpfEdit(
    series="Série synthétique",
    series_index="3",
    rights="CC BY-SA 4.0",
    source="https://example.org/livre",
)
DC = "{http://purl.org/dc/elements/1.1/}"
OPF_NS = "{http://www.idpf.org/2007/opf}"
META = f"{OPF_NS}meta"


def fingerprint(root: Element) -> list[tuple[str, tuple[tuple[str, str], ...], str]]:
    metadata = root.find(f"{OPF_NS}metadata")
    assert metadata is not None
    return [
        (child.tag, tuple(sorted(child.attrib.items())), (child.text or "").strip())
        for child in metadata
    ]


def repackaged(source: bytes, edit: OpfEdit = EDIT) -> tuple[bytes, bytes]:
    out = io.BytesIO()
    with safe_open_archive(io.BytesIO(source), name="b.epub") as archive:
        result = repackage_epub(archive, edit, out)
    return out.getvalue(), result.original_opf


def opf_of(epub_bytes: bytes) -> bytes:
    with safe_open_archive(io.BytesIO(epub_bytes), name="b.epub") as archive:
        return bytes(archive.read("OEBPS/content.opf"))


@pytest.mark.req("EF-EXP-033")
def test_epub_opf_only_listed_elements_written() -> None:
    before = safe_parse_xml(opf())
    rewritten = rewrite_opf(opf(), EDIT).opf
    old, new = fingerprint(before), fingerprint(safe_parse_xml(rewritten))
    assert all(item in new for item in old)  # nothing was removed or altered
    added = [item for item in new if item not in old]
    names = {
        (tag, dict(attrs).get("name") or dict(attrs).get("property")) for tag, attrs, _ in added
    }
    assert names <= {
        (META, "calibre:series"),
        (META, "calibre:series_index"),
        (META, "belongs-to-collection"),
        (META, "collection-type"),
        (META, "group-position"),
        (f"{DC}rights", None),
        (f"{DC}source", None),
    }
    assert (META, "calibre:series") in names
    # everything outside <metadata> is copied as it was
    text, after = opf().decode(), rewritten.decode()
    assert after.startswith(text[: text.index("  </metadata>")])
    assert after.endswith(text[text.index("</metadata>") :])


@pytest.mark.req("EF-EXP-033")
def test_epub_opf_rights_and_source_only_when_absent() -> None:
    present = opf(
        extra_metadata="    <dc:rights>Public domain</dc:rights>\n"
        "    <dc:source>urn:isbn:0000</dc:source>\n"
    )
    after = safe_parse_xml(rewrite_opf(present, EDIT).opf)
    assert [e.text for e in after.iter(f"{DC}rights")] == ["Public domain"]
    assert [e.text for e in after.iter(f"{DC}source")] == ["urn:isbn:0000"]


@pytest.mark.req("EF-EXP-033")
def test_epub_opf_series_replaced_not_duplicated() -> None:
    first = rewrite_opf(opf(), EDIT).opf
    again = rewrite_opf(first, OpfEdit(series="Autre", series_index="4")).opf
    metas = [m for m in safe_parse_xml(again).iter(META)]
    named = {m.get("name"): m.get("content") for m in metas if m.get("name")}
    assert [m.get("name") for m in metas].count("calibre:series") == 1
    assert named["calibre:series"] == "Autre"
    assert named["calibre:series_index"] == "4"
    collections = [m.text for m in metas if m.get("property") == "belongs-to-collection"]
    assert collections == ["Autre"]


@pytest.mark.req("EF-EXP-033")
def test_epub_opf_values_are_escaped() -> None:
    nasty = OpfEdit(series='A & B <i> "q"', series_index="1", rights="x </dc:rights><y/>")
    root = safe_parse_xml(rewrite_opf(opf(), nasty).opf)
    assert any(m.attrib.get("content") == 'A & B <i> "q"' for m in root.iter(META))
    assert [e.text for e in root.iter(f"{DC}rights")] == ["x </dc:rights><y/>"]


@pytest.mark.req("EF-EXP-033")
def test_page_progression_direction_preserved() -> None:
    root = safe_parse_xml(opf_of(repackaged(epub())[0]))
    spine = root.find(f"{OPF_NS}spine")
    assert spine is not None
    assert spine.get("page-progression-direction") == "rtl"
    named = {m.get("name"): m.get("content") for m in root.iter(META) if m.get("name")}
    assert named["primary-writing-mode"] == "vertical-rl"  # the writing mode too (EF-EXP-033)
    layout = [m.text for m in root.iter(META) if m.get("property") == "rendition:layout"]
    assert layout == ["pre-paginated"]


@pytest.mark.req("EF-EXP-033")
def test_epub_accessibility_metadata_preserved() -> None:
    root = safe_parse_xml(opf_of(repackaged(epub())[0]))
    access = {
        m.get("property"): m.text
        for m in root.iter(META)
        if (m.get("property") or "").startswith("schema:")
    }
    assert access == {
        "schema:accessMode": "textual",
        "schema:accessibilityFeature": "tableOfContents",
        "schema:accessibilitySummary": "Synthetic summary.",
    }


@pytest.mark.req("EF-EXP-033")
def test_epub_passthrough_preserves_schema_accessibility_meta() -> None:
    rewritten = rewrite_opf(opf(), EDIT).opf.decode().splitlines()
    for line in opf().decode().splitlines():
        if "schema:access" in line:
            assert line in rewritten  # the very same line, byte for byte


@pytest.mark.req("EF-EXP-033", "EF-BIB-041")
def test_opf_edit_preserves_all_other_entries_bytewise() -> None:
    source = epub()
    out, _ = repackaged(source)
    with zipfile.ZipFile(io.BytesIO(source)) as before, zipfile.ZipFile(io.BytesIO(out)) as after:
        assert before.namelist() == after.namelist()  # same entries, same order, `mimetype` first
        assert after.infolist()[0].compress_type == zipfile.ZIP_STORED
        for name in before.namelist():
            if name == "OEBPS/content.opf":
                assert before.read(name) != after.read(name)
            else:
                assert before.read(name) == after.read(name), name
        assert after.read("META-INF/rights.xml") == RIGHTS_XML
        assert after.read("OEBPS/images/wm.bin") == WATERMARK


@pytest.mark.req("EF-BIB-048", "EF-EXP-033")
def test_original_opf_returned_for_the_meta_copy_and_absent_from_the_file() -> None:
    source = epub()
    out, original = repackaged(source)
    assert original == opf()
    with zipfile.ZipFile(io.BytesIO(out)) as after:
        assert len(after.namelist()) == len(zipfile.ZipFile(io.BytesIO(source)).namelist())
        assert all(original not in after.read(n) for n in after.namelist())


@pytest.mark.req("ENF-I18N-003", "EF-EXP-033")
@pytest.mark.parametrize("language", ["zh-Hant", "es-419"])
def test_epub_language_golden_zh_hant_es_419(language: str) -> None:
    rewritten = opf_of(repackaged(epub(opf(language=language, direction=None)))[0])
    root = safe_parse_xml(rewritten)
    assert [e.text for e in root.iter(f"{DC}language")] == [language]
    target = GOLDEN / f"language_{language}.opf"
    if os.environ.get("SHELFHOLD_UPDATE_GOLDEN") == "1":
        target.write_bytes(rewritten)
    assert target.read_bytes() == rewritten


@pytest.mark.req("ENF-I18N-003")
def test_no_translation_call_in_exporters() -> None:
    """The tokens Kavita reads never go through the catalogue (Z3-c): no `t()` in an exporter."""
    offenders = []
    for folder in ("export", "infrastructure/packaging"):
        for path in (SRC / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                call = isinstance(node, ast.Call)
                if call and isinstance(node.func, ast.Name) and node.func.id == "t":
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


@pytest.mark.req("EF-EXP-033")
def test_epub2_gets_calibre_series_and_no_collection() -> None:
    root = safe_parse_xml(rewrite_opf(opf(version="2.0"), EDIT).opf)
    props = [m.get("property") for m in root.iter(META)]
    assert "belongs-to-collection" not in props
    assert any(m.get("name") == "calibre:series" for m in root.iter(META))


@pytest.mark.req("EF-EXP-033")
def test_collection_of_type_set_is_the_books_own_and_kept() -> None:
    own = (
        '    <meta property="belongs-to-collection" id="s1">Anthologie</meta>\n'
        '    <meta refines="#s1" property="collection-type">set</meta>\n'
    )
    root = safe_parse_xml(rewrite_opf(opf(extra_metadata=own), EDIT).opf)
    collections = [m.text for m in root.iter(META) if m.get("property") == "belongs-to-collection"]
    assert sorted(c or "" for c in collections) == ["Anthologie", "Série synthétique"]


@pytest.mark.req("EF-EXP-033")
def test_crlf_and_prefixed_package_are_edited_in_place() -> None:
    text = opf().decode().replace("\n", "\r\n")
    out = rewrite_opf(text.encode(), EDIT).opf.decode()
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")
    prefixed = (
        '<opf:package xmlns:opf="http://www.idpf.org/2007/opf" version="2.0">'
        '<opf:metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
        "</opf:metadata></opf:package>"
    )
    root = safe_parse_xml(rewrite_opf(prefixed.encode(), EDIT).opf)
    assert any(m.get("name") == "calibre:series" for m in root.iter(META))


@pytest.mark.req("EF-EXP-033")
def test_unsafe_or_non_utf8_opf_is_refused() -> None:
    from shelfhold.infrastructure.packaging.epub import OpfError
    from shelfhold.infrastructure.xml import UnsafeXmlError

    with pytest.raises(UnsafeXmlError):
        rewrite_opf(b'<!DOCTYPE x [<!ENTITY a "b">]><package xmlns="x"/>', EDIT)
    with pytest.raises(OpfError):
        rewrite_opf(opf().replace(b"utf-8", b"latin-1").replace("é".encode(), b"\xe9"), EDIT)


AGE_FORMS = {
    "typical_age_range": '<meta property="schema:typicalAgeRange">16+</meta>',
    "typical_age_range_case_and_prefix": '<meta property="S:TYPICALAGERANGE">16</meta>',
    "subject_18_plus": "<dc:subject>18+</dc:subject>",
    "subject_renamed_prefix": (
        '<d:subject xmlns:d="http://purl.org/dc/elements/1.1/">18 +</d:subject>'
    ),
    "subject_mature": "<dc:subject>Mature</dc:subject>",
    "calibre_rating": '<meta name="calibre:rating" content="8"/>',
    "calibre_rating_single_quotes": "<meta name='Calibre:Rating' content='8'/>",
    "ibooks_age_rating": '<meta property="ibooks:age-rating">17+</meta>',
    "ibooks_name_form": '<meta name="ibooks:age-rating" content="17+"/>',
    "opf_prefixed_meta": '<opf:meta xmlns:opf="http://www.idpf.org/2007/opf" name="age_rating" '
    'content="18"/>',
    "refines_of_an_age": '<meta property="schema:typicalAgeRange" id="ag">16+</meta>\n'
    '    <meta refines="#ag" property="role">x</meta>\n'
    '    <meta refines="#ag" id="ag2" property="source">y</meta>\n'
    '    <meta refines="#ag2" property="z">w</meta>',
}


@pytest.mark.req("EF-EXP-033")
@pytest.mark.parametrize("form", list(AGE_FORMS.values()), ids=list(AGE_FORMS))
@pytest.mark.parametrize("edit", [EDIT, OpfEdit()], ids=["edit", "no_edit"])
def test_age_already_in_the_origin_opf_is_removed(form: str, edit: OpfEdit) -> None:
    source = opf(extra_metadata=f"    {form}\n")
    written = rewrite_opf(source, edit)
    assert written.changed
    text = written.opf.decode()
    for needle in ("16+", "18+", "18 +", "agerange", "age-rating", "age_rating", "rating", "mature",
                   'refines="#ag'):  # fmt: skip
        assert needle not in text.lower(), needle
    assert "<dc:subject>Fiction</dc:subject>" in text  # the book's own subject stays
    root = safe_parse_xml(written.opf)
    assert 'page-progression-direction="rtl"' in text
    assert any(m.get("property") == "schema:accessMode" for m in root.iter(META))


@pytest.mark.req("EF-EXP-033")
def test_origin_opf_without_age_and_without_edit_is_unchanged() -> None:
    assert rewrite_opf(opf(), OpfEdit()).changed is False


@pytest.mark.req("EF-EXP-033")
def test_check_refuses_anything_outside_the_list() -> None:
    from shelfhold.infrastructure.packaging.epub import OpfError
    from shelfhold.infrastructure.packaging.epub.opf import _check

    before = safe_parse_xml(opf())
    good = rewrite_opf(opf(), EDIT).opf.decode()
    _check(before, safe_parse_xml(good.encode()), EDIT)  # the real rewrite passes
    wrong_edit = (
        # not on the list: an age, in every spelling, even if it was not in the origin
        '<meta property="schema:typicalAgeRange">16+</meta>',
        "<dc:subject>18+</dc:subject>",
        '<meta name="calibre:rating" content="1"/>',
        '<meta property="ibooks:age-rating">17+</meta>',
        # not on the list: other elements, look-alikes, renamed namespaces
        '<meta name="Calibre:Series" content="x"/>',
        '<meta name="calibre:series" content="x" extra="1"/>',
        '<meta name="shelfhold:anything" content="1"/>',
        '<x:meta xmlns:x="urn:evil" name="calibre:series" content="x"/>',
        '<d:rights xmlns:d="urn:evil">x</d:rights>',
        '<dc:rights lang="x">x</dc:rights>',
        "<dc:creator>Someone</dc:creator>",
        # refines pointing somewhere that is not the collection written here
        '<meta refines="#id" property="group-position">3</meta>',
        '<meta refines="#nope" property="collection-type">series</meta>',
        '<meta refines="#coll1" property="role">x</meta>',
    )
    for extra in wrong_edit:
        bad = good.replace("</metadata>", f"{extra}</metadata>")
        with pytest.raises(OpfError):
            _check(before, safe_parse_xml(bad.encode()), EDIT)
    # something of the book's own going missing is refused too
    with pytest.raises(OpfError):
        _check(
            before,
            safe_parse_xml(good.replace("<dc:title>Livre synthétique</dc:title>", "").encode()),
            EDIT,
        )
    # with no series in the edit, even the series forms are off the list
    with pytest.raises(OpfError):
        _check(before, safe_parse_xml(good.encode()), OpfEdit())
