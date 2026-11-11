# ruff: noqa: E501
"""EF-BIB-051 (2), (7), (8) and ENF-SEC-015: xhtml-neutralizer@v1 against the trapped-EPUB corpus."""

from __future__ import annotations

import ast
import contextlib
import inspect
from io import BytesIO
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust import (
    DERIVED_BY,
    DERIVED_HASH_BY,
    NeutralizationError,
    neutralize,
    neutralize_epub,
)
from shelfhold.sdk.testing.corpus.trapped_epubs import (
    CHAPTER,
    STYLESHEET,
    VECTOR_FAMILIES,
    Member,
    Trap,
    all_traps,
    base_traps,
    epub_bytes,
    find_active_css,
    find_active_patterns,
    find_remote_references,
    mutation_traps,
    xhtml,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
_TRAPS = all_traps()


def _run(data: bytes) -> tuple[bytes, object]:
    out = BytesIO()
    result = neutralize_epub(BytesIO(data), out)
    return out.getvalue(), result


def _entries(data: bytes) -> dict[str, bytes]:
    with safe_open_archive(BytesIO(data), name="x.epub") as archive:
        return {e.name: bytes(archive.read(e.name)) for e in archive.entries if not e.is_dir}


def _names(data: bytes) -> list[str]:
    with safe_open_archive(BytesIO(data), name="x.epub") as archive:
        return [e.name for e in archive.entries]


def _scan(name: str, content: bytes) -> list[str]:
    """Every entry is looked at, whatever its name; the OPF is looked at for remote references."""
    if name.lower().endswith(".opf"):
        return find_remote_references(content)
    if name.lower().endswith(".css"):
        return find_active_css(content)
    return find_active_patterns(content)


@pytest.mark.req("EF-BIB-051", "ENF-SEC-015")
@pytest.mark.parametrize("trap", _TRAPS, ids=[t.name for t in _TRAPS])
def test_untrusted_epub_scripts_and_handlers_neutralized_before_export(trap: Trap) -> None:
    """Corpus: every trap is refused with a stable reason or comes out without an active pattern."""
    if trap.outcome == "refused":
        with pytest.raises(NeutralizationError) as caught:
            _run(trap.epub)
        assert caught.value.code == "content.untrusted_entry"
        assert trap.reason in caught.value.reason
        return
    derived, _ = _run(trap.epub)
    entries = _entries(derived)
    findings = {n: _scan(n, c) for n, c in entries.items()}
    assert not {n: f for n, f in findings.items() if f}, findings
    assert trap.entry in entries


@pytest.mark.req("EF-BIB-051")
def test_corpus_covers_every_vector_family_and_the_mutations() -> None:
    families = {t.vector for t in base_traps()}
    assert families >= set(VECTOR_FAMILIES) - {"mutation"}
    assert len(mutation_traps()) >= 150
    assert len({t.name for t in _TRAPS}) == len(_TRAPS)


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    "document",
    [
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><script>1</script></body></html>',
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p onclick="1" onerror="1">x</p></body></html>',
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><a href="javascript:1">x</a></body></html>',
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><iframe src="a"></iframe></body></html>',
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><style>@import "a";</style></head><body/></html>',
    ],
)
def test_the_detector_sees_what_it_is_meant_to_see(document: bytes) -> None:
    """The corpus oracle is only worth something if it catches the untouched vectors."""
    assert find_active_patterns(document)


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize("trap", base_traps()[:60:3], ids=lambda t: t.name)
def test_neutralizer_deterministic_same_derived_hash(trap: Trap) -> None:
    if trap.outcome == "refused":
        pytest.skip("nothing is produced")
    first, one = _run(trap.epub)
    second, two = _run(trap.epub)
    assert first == second
    assert one.derived_hash == two.derived_hash  # type: ignore[attr-defined]
    assert one.derived_by == DERIVED_BY == "xhtml-neutralizer@v1"  # type: ignore[attr-defined]
    assert one.derived_hash_by == DERIVED_HASH_BY  # type: ignore[attr-defined]
    assert one.derived_hash.startswith("sha256:")  # type: ignore[attr-defined]


@pytest.mark.req("EF-BIB-051")
def test_neutralizer_is_a_pure_function_of_bytes_and_kind() -> None:
    doc = xhtml('<p onclick="x">a</p><script>1</script>')
    assert neutralize(doc, "xhtml") == neutralize(doc, "xhtml")
    assert list(inspect.signature(neutralize).parameters) == ["data", "kind"]
    tree = ast.parse((SRC / "infrastructure/content_trust/xhtml_neutralizer.py").read_text("utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Global)]
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not imported & {"random", "time", "datetime", "os", "uuid", "secrets", "socket"}


@pytest.mark.req("EF-BIB-051")
def test_a_clean_epub_comes_out_unchanged_and_its_derived_hash_is_the_original_one() -> None:
    messy = epub_bytes(
        xhtml('<h1 epub:type="title">T</h1><p>a <a href="https://x.example/">b</a></p>')
    )
    first, one = _run(messy)  # the writer's own form is a fixed point of the function
    assert one.changed is True  # type: ignore[attr-defined]
    second, result = _run(first)
    assert second == first
    assert _names(second) == _names(messy)
    assert result.changed is False  # type: ignore[attr-defined]
    assert result.derived_hash == result.original_hash == one.derived_hash  # type: ignore[attr-defined]


@pytest.mark.req("EF-BIB-051")
def test_neutralized_epub_keeps_content_and_changes_the_hash() -> None:
    page = xhtml('<h1>Title</h1><p class="c">Some <em>text</em> here.</p><p onclick="1">x</p>')
    original = epub_bytes(page)
    derived, result = _run(original)
    body = _entries(derived)[CHAPTER].decode()
    assert "Title" in body
    assert "Some <em>text</em> here." in body
    assert 'class="c"' in body
    assert result.changed is True  # type: ignore[attr-defined]
    assert result.derived_hash != result.original_hash  # type: ignore[attr-defined]


@pytest.mark.req("EF-BIB-051")
def test_publisher_and_watermarked_epub_untouched_outside_opf() -> None:
    """Whatever the neutralizer does not own is copied byte for byte, in the same order."""
    mark = b'<?xml version="1.0"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0"/>'
    extra = [
        Member("OEBPS/font.woff", b"wOFF" + bytes(range(256)) * 4, deflate=False),
        Member("OEBPS/notes.xml", b"<notes>plain\r\n</notes>", deflate=True),
        Member("OEBPS/images/a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 64),
    ]
    original = epub_bytes(xhtml('<p onclick="1">x</p>'), extra=extra)
    derived, _ = _run(original)
    before, after = _entries(original), _entries(derived)
    assert _names(derived) == _names(original)
    pages = (".xhtml", ".css", ".svg")
    for name in before:
        if not name.endswith(pages):
            assert after[name] == before[name], name
    assert after["OEBPS/font.woff"] == extra[0].data
    del mark


@pytest.mark.req("EF-BIB-051")
def test_neutralizer_never_rewrites_opf_or_meta_inf() -> None:
    hostile_opf_like = (
        b'<?xml version="1.0"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:contributor>'
        b"&lt;script&gt;alert(1)&lt;/script&gt; watermark:abc123</dc:contributor></metadata></package>"
    )
    extra = [
        Member("META-INF/metadata.xml", b'<metadata note="x"/>'),  # a known container file
        Member("OEBPS/other.opf", hostile_opf_like),
    ]
    original = epub_bytes(xhtml("<p>x</p>"), extra=extra)
    derived, _ = _run(original)
    before, after = _entries(original), _entries(derived)
    for name in before:
        if name.startswith("META-INF/") or name.endswith(".opf"):
            assert after[name] == before[name], name


@pytest.mark.req("EF-BIB-051")
def test_mimetype_stays_first_and_stored() -> None:
    derived, _ = _run(epub_bytes(xhtml("<script>1</script><p>x</p>")))
    with safe_open_archive(BytesIO(derived), name="x.epub") as archive:
        assert archive.entries[0].name == "mimetype"
        assert archive.entries[0].compressed_size == archive.entries[0].size
        assert bytes(archive.read("mimetype")) == b"application/epub+zip"


@pytest.mark.req("EF-BIB-051", "ENF-SEC-015")
def test_heuristic_watermark_never_bypasses_neutralizer() -> None:
    """A trapped EPUB that imitates a watermark is neutralized: the function takes no trust input."""
    assert list(inspect.signature(neutralize_epub).parameters)[:2] == ["source", "out"]
    assert not {"trust", "watermarked", "publisher", "content_trust"} & set(
        inspect.signature(neutralize_epub).parameters
    )
    fake = (
        b'<?xml version="1.0"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:contributor>Licensed to reader@example.org order 1234567</dc:contributor>"
        b"</metadata></package>"
    )
    original = epub_bytes(xhtml('<p>Licensed to reader@example.org</p><script>1</script>'),
                          extra=[Member("OEBPS/mark.opf", fake)])  # fmt: skip
    derived, _ = _run(original)
    assert not find_active_patterns(_entries(derived)[CHAPTER])


@pytest.mark.req("EF-BIB-051")
def test_benign_markup_survives() -> None:
    page = xhtml(
        '<div id="d" class="k" xml:lang="fr"><h2>H</h2><ul><li>a</li></ul>'
        '<img src="images/p.png" alt="p" width="3" height="3"/><br/>'
        '<table><tr><td colspan="2">c</td></tr></table>'
        '<a href="ch2.xhtml#x">in</a>'
        '<svg:svg viewBox="0 0 4 4"><svg:path d="M0 0L4 4" fill="red"/><svg:use href="#a"/></svg:svg>'
        '<p style="color: red; margin: 0">s</p></div>',
        '<link rel="stylesheet" type="text/css" href="style.css"/>'
        "<style>p { background: url(images/p.png) }</style>",
    )
    body = _entries(_run(epub_bytes(page))[0])[CHAPTER].decode()
    for kept in ("<h2>H</h2>", 'src="images/p.png"', 'href="ch2.xhtml#x"',
                 'd="M0 0L4 4"', 'href="style.css"', "url(images/p.png)", "color: red"):  # fmt: skip
        assert kept in body, kept
    assert not find_active_patterns(body.encode())


@pytest.mark.req("EF-BIB-051")
def test_stylesheet_file_keeps_safe_rules_only() -> None:
    css = b"p { color: red }\nh1 { background: url(images/h.png) }\n"
    out = _entries(_run(epub_bytes(xhtml("<p>x</p>"), css=css))[0])[STYLESHEET]
    assert out == css
    bad = b"@import url(http://evil.example/x.css);\np { color: red }\n"
    out = _entries(_run(epub_bytes(xhtml("<p>x</p>"), css=bad))[0])[STYLESHEET]
    assert not find_active_css(out)


@pytest.mark.req("EF-BIB-051")
def test_html_named_entities_of_an_xhtml_doctype_are_kept() -> None:
    doc = (
        b'<?xml version="1.0"?>\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
        b'"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
        b"<body><p>a&nbsp;b &copy; &amp; &lt;</p></body></html>"
    )
    out = neutralize(doc, "xhtml").decode()
    assert "a\u00a0b \u00a9 &amp; &lt;" in out
    assert "DOCTYPE" not in out


@pytest.mark.req("EF-BIB-051", "ENF-SEC-015")
def test_nh3_is_called_on_epub_xhtml_only_by_the_neutralizer() -> None:
    owners = sorted(
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if "import nh3" in p.read_text("utf-8") or "from nh3" in p.read_text("utf-8")
    )
    assert owners == [
        "infrastructure/content_trust/sanitize.py",
        "infrastructure/content_trust/xhtml_neutralizer.py",
    ]


# --- Property: whatever the input, no active pattern comes out ------------------------------------

_TAGS = st.sampled_from(["p", "div", "a", "img", "script", "SCRIPT", "iframe", "svg", "use", "set",
                         "animate", "foreignObject", "form", "link", "style", "object", "embed",
                         "base", "meta", "span", "table", "td", "title", "noscript", "template",
                         "svg:script", "x:iframe"])  # fmt: skip
_ATTRS = st.sampled_from(["href", "src", "xlink:href", "onclick", "ONERROR", "onload", "style",
                          "data", "action", "formaction", "class", "id", "http-equiv", "content",
                          "srcset", "poster", "rel", "type", "values", "to", "attributeName"])  # fmt: skip
_SCHEMES = st.sampled_from(["javascript", "JavaScript", "vbscript", "data", "http", "https", "//",
                            "&#106;avascript", "java&#9;script", "jav&#x0A;ascript", "\u00a0javascript",
                            "", "x", "#", "ftp"])  # fmt: skip
_VALUES = st.builds(
    lambda scheme, rest: f"{scheme}:{rest}" if scheme not in ("//", "") else f"{scheme}{rest}",
    _SCHEMES,
    st.sampled_from(["alert(1)", "//evil.example/x", "text/html,x", "a.png", "url(http://e/x)"]),
) | st.sampled_from(["url(http://e.example/x)", "@import 'x'", "u\\72l(http://e/)", "expression(1)",
                     "background:url(//e.example/x)", "a.png", "refresh", "0;url=http://e/x"])  # fmt: skip


@st.composite
def _fragment(draw: st.DrawFn, depth: int = 0) -> str:
    tag = draw(_TAGS)
    attrs = "".join(f' {draw(_ATTRS)}="{draw(_VALUES)}"' for _ in range(draw(st.integers(0, 3))))
    inner = ""
    if depth < 3:
        inner = "".join(draw(_fragment(depth + 1)) for _ in range(draw(st.integers(0, 2))))
    text = draw(st.sampled_from(["", "text", "alert(1)", "p{background:url(http://e/x)}", "&amp;"]))
    return f'<{tag}{attrs} xmlns:x="http://www.w3.org/1999/xhtml">{text}{inner}</{tag}>'


@pytest.mark.req("EF-BIB-051", "ENF-SEC-015")
@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_fragment(), min_size=1, max_size=4))
def test_property_no_output_holds_an_active_pattern(fragments: list[str]) -> None:
    document = xhtml("".join(fragments)).replace(b"xmlns:svg=", b"xmlns:svg=")
    try:
        out = neutralize(document, "xhtml")
    except NeutralizationError:
        return  # refused: nothing is produced
    assert find_active_patterns(out) == []
    assert neutralize(document, "xhtml") == out  # deterministic
    assert neutralize(out, "xhtml") == out  # idempotent: a derivative is a fixed point


_CSS_PARTS = st.sampled_from([
    "p", "a > b", "{", "}", ";", "color:red", "background:url(http://e.example/x)", "url(a.png)",
    "@import 'x'", "@IMPORT", "u\\72l(", "\\75rl(//e/", "expression(1)", "/*", "*/", "'", '"',
    "url(", ")", "behavior:url(x)", "@font-face", "src:url(//e/f)", "image-set(", "&", "<", "\n",
    "-moz-binding", "content:'\\201C'", "url(  'data:text/html,x' )", "url(#a)", "javascript:",
])  # fmt: skip


@pytest.mark.req("EF-BIB-051", "ENF-SEC-015")
@settings(max_examples=400, deadline=None)
@given(st.lists(_CSS_PARTS, min_size=1, max_size=12))
def test_property_no_stylesheet_keeps_a_fetching_or_scripting_rule(parts: list[str]) -> None:
    sheet = " ".join(parts).encode()
    out = neutralize(sheet, "css")
    assert find_active_css(out) == []
    assert neutralize(out, "css") == out
    page = xhtml(
        "<p>x</p>",
        "<style>" + sheet.decode().replace("<", "&lt;").replace("&", "&amp;") + "</style>",
    )
    with contextlib.suppress(NeutralizationError):
        assert find_active_patterns(neutralize(page, "xhtml")) == []


@pytest.mark.req("EF-BIB-051", "ENF-SEC-015")
@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_fragment(), min_size=1, max_size=3))
def test_property_no_standalone_svg_keeps_an_active_pattern(fragments: list[str]) -> None:
    document = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        + "".join(fragments) + "</svg>"
    ).encode()  # fmt: skip
    try:
        out = neutralize(document, "svg")
    except NeutralizationError:
        return
    assert find_active_patterns(out) == []
    assert neutralize(out, "svg") == out


@pytest.mark.req("EF-BIB-051", "ENF-SEC-015")
def test_corpus_green_under_the_kit() -> None:
    from shelfhold.sdk.testing import run_trapped_epub_corpus

    def derive(epub: bytes) -> dict[str, bytes]:
        out = BytesIO()
        neutralize_epub(BytesIO(epub), out)
        return _entries(out.getvalue())

    report = run_trapped_epub_corpus(derive)
    assert report.ok, [(c.name, c.detail) for c in report.failures][:5]
    assert len(report.checks) == len(all_traps())

    def identity(epub: bytes) -> dict[str, bytes]:
        return _entries(epub)  # a "neutralizer" that does nothing must fail the kit

    broken = run_trapped_epub_corpus(identity)
    assert not broken.ok
    assert len(broken.failures) > 100


@pytest.mark.req("EF-BIB-051")
def test_directory_entries_and_stored_entries_are_copied_in_place() -> None:
    original = epub_bytes(
        xhtml("<p>x</p>"),
        extra=[
            Member("OEBPS/images/", b""),
            Member("OEBPS/images/a.png", b"\x89PNG\r\n\x1a\n", False),
        ],
    )
    derived, _ = _run(original)
    assert _names(derived) == _names(original)
    assert _entries(derived)["OEBPS/images/a.png"] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.req("EF-BIB-051")
def test_unknown_kind_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        neutralize(b"", "html")  # type: ignore[arg-type]


@pytest.mark.req("EF-BIB-051", "ENF-SEC-015")
@pytest.mark.parametrize(
    ("name", "media", "head", "expected"),
    [
        ("OEBPS/a.xhtml", "", b"", "xhtml"),
        ("OEBPS/a.svg", "", b"", "svg"),
        ("OEBPS/a.css", "", b"", "css"),
        ("OEBPS/a.dat", "text/html", b"<p>x</p>", "xhtml"),
        ("OEBPS/a", "text/html", b"<p>x</p>", "xhtml"),
        ("OEBPS/a.dat", "application/xhtml+xml; charset=UTF-8", b"x", "xhtml"),
        ("OEBPS/a", "text/xml", b"<html><script/></html>", "page_in_xml"),
        ("OEBPS/a.bin", "application/xml", b"<notes/>", "unexpected_entry"),
        ("OEBPS/a.xml", "", b"<html/>", "page_in_xml"),
        ("OEBPS/a.xml", "", b"<notes/>", "copy"),
        ("OEBPS/a.ncx", "", b"<ncx/>", "copy"),
        ("OEBPS/a.png", "", b"\x89PNG\r\n\x1a\n", "copy"),
        ("OEBPS/a.png", "", b"<svg/>", "magic_mismatch"),
        ("OEBPS/a.woff2", "font/woff2", b"wOF2", "copy"),
        ("OEBPS/a.png", "text/html", b"\x89PNG\r\n\x1a\n", "type_mismatch"),
        ("OEBPS/a.xhtml", "image/png", b"x", "type_mismatch"),
        ("OEBPS/a.txt", "", b"plain", "unexpected_entry"),
        ("OEBPS/a.js", "", b"var x = 1;", "unexpected_entry"),
        ("OEBPS/a.dat", "", b"x", "unexpected_entry"),
        ("OEBPS/a", "", b"  <!DOCTYPE html><script>1</script>", "unexpected_entry"),
        ("OEBPS/a.svgz", "", b"\x1f\x8b", "unexpected_entry"),
        ("OEBPS/a.mp3", "audio/mpeg", b"ID3", "unexpected_entry"),
    ],
)
def test_entry_routing_is_a_closed_list_of_categories(
    name: str, media: str, head: bytes, expected: str
) -> None:
    from shelfhold.infrastructure.content_trust.epub_scan import _classify, _manifest

    items = f'<item id="x" href="{name.rsplit("/", 1)[-1]}" media-type="{media}"/>' if media else ""
    epub = epub_bytes(xhtml("<p>x</p>"), extra=[Member(name, head)], opf_extra=items)
    with safe_open_archive(BytesIO(epub), name="x.epub") as archive:
        opfs, declared = _manifest(archive)
        if expected in ("xhtml", "svg", "css", "copy"):
            assert _classify(archive, name, opfs, declared) == expected
        else:
            with pytest.raises(NeutralizationError) as caught:
                _classify(archive, name, opfs, declared)
            assert caught.value.reason == expected


@pytest.mark.req("EF-BIB-051", "ENF-SEC-015")
@pytest.mark.parametrize("chapter", ["Text/ch1.xhtml", "Part%201%3A%20Intro.xhtml", "a%20b.xhtml"])
def test_a_standard_epub3_still_passes(chapter: str) -> None:
    """Non-regression (counter-review): nav, NCX, cover SVG, fonts, css urls, encoded names."""
    from shelfhold.sdk.testing.corpus.trapped_epubs import standard_epub3

    derived, result = _run(standard_epub3(chapter))
    entries = _entries(derived)
    original = _entries(standard_epub3(chapter))
    for name in ("OEBPS/Fonts/Body.otf", "OEBPS/Fonts/Body.woff2", "OEBPS/Fonts/Old.ttf",
                 "OEBPS/Images/cover.jpg", "OEBPS/toc.ncx", "OEBPS/content.opf"):  # fmt: skip
        assert entries[name] == original[name], name
    assert entries["OEBPS/Styles/style.css"].count(b"url(") == 3
    assert b"cover.jpg" in entries["OEBPS/Text/cover.xhtml"]
    assert not result.derived_hash.endswith("0" * 8)


@pytest.mark.req("EF-BIB-051", "ENF-SEC-015")
def test_outbound_links_lose_their_href_and_keep_their_text() -> None:
    page = xhtml(
        '<p><a href="http://e.example/x">one</a> <a href="https://e.example/">two</a> '
        '<a href="mailto:a@b.example">three</a> <a href="ch2.xhtml#n">four</a></p>'
    )
    out = neutralize(page, "xhtml").decode()
    for text in ("one", "two", "three", "four"):
        assert text in out
    assert 'href="ch2.xhtml#n"' in out
    assert "http" not in out.split("<body>")[1]
    assert "mailto" not in out


@pytest.mark.req("EF-BIB-051")
def test_the_opf_is_refused_not_rewritten_when_it_loads_a_remote_resource() -> None:
    bad = epub_bytes(
        xhtml("<p>x</p>"),
        opf_extra='<item id="c" href="https://e.example/c.png" media-type="image/png"/>',
    )
    with pytest.raises(NeutralizationError) as caught:
        _run(bad)
    assert caught.value.reason == "remote_reference"
    assert find_remote_references(
        b'<package><manifest><item href="https://e.example/c.png"/></manifest></package>'
    )
