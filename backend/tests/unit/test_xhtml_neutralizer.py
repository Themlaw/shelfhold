"""EF-BIB-051 (2), (7), (8) and ENF-SEC-015: xhtml-neutralizer@v1 against the trapped-EPUB corpus."""

from __future__ import annotations

import ast
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
    if name.endswith(".css"):
        return find_active_css(content)
    if name.lower().endswith((".xhtml", ".html", ".htm", ".svg", ".xml", ".dat")):
        return find_active_patterns(content)
    return []


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
    findings = {n: _scan(n, c) for n, c in entries.items() if n not in ("OEBPS/content.opf",)}
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
    clean = epub_bytes(xhtml('<h1 epub:type="title">T</h1><p>a <a href="https://x.example/">b</a></p>'))
    derived, result = _run(clean)
    assert _names(derived) == _names(clean)
    assert result.changed is False  # type: ignore[attr-defined]
    assert result.derived_hash == result.original_hash  # type: ignore[attr-defined]


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
        Member("OEBPS/notes.txt", b"plain notes\r\n", deflate=True),
        Member("OEBPS/images/a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 64),
    ]
    original = epub_bytes(xhtml('<p onclick="1">x</p>'), extra=extra)
    derived, _ = _run(original)
    before, after = _entries(original), _entries(derived)
    assert _names(derived) == _names(original)
    for name in before:
        if name != CHAPTER:
            assert after[name] == before[name], name
    del mark


@pytest.mark.req("EF-BIB-051")
def test_neutralizer_never_rewrites_opf_or_meta_inf() -> None:
    hostile_opf_like = (
        b'<?xml version="1.0"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:contributor>'
        b"&lt;script&gt;alert(1)&lt;/script&gt; watermark:abc123</dc:contributor></metadata></package>"
    )
    extra = [
        Member("META-INF/encryption.xml", b'<encryption onload="x"/>'),
        Member("META-INF/page.xhtml", xhtml('<p onclick="1">m</p>')),
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
    derived, _ = _run(epub_bytes(xhtml('<script>1</script><p>x</p>')))
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
        '<a href="ch2.xhtml#x">in</a> <a href="mailto:a@b.example">m</a>'
        '<svg:svg viewBox="0 0 4 4"><svg:path d="M0 0L4 4" fill="red"/><svg:use href="#a"/></svg:svg>'
        '<p style="color: red; margin: 0">s</p></div>',
        '<link rel="stylesheet" type="text/css" href="style.css"/>'
        "<style>p { background: url(images/p.png) }</style>",
    )
    body = _entries(_run(epub_bytes(page))[0])[CHAPTER].decode()
    for kept in ("<h2>H</h2>", 'src="images/p.png"', 'href="ch2.xhtml#x"', 'href="mailto:a@b.example"',
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
    assert "a b © &amp; &lt;" in out
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
                            "&#106;avascript", "java&#9;script", "jav&#x0A;ascript", " javascript",
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
    attrs = "".join(
        f' {draw(_ATTRS)}="{draw(_VALUES)}"' for _ in range(draw(st.integers(0, 3)))
    )
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
