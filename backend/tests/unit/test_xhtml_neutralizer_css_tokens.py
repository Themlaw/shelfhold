# ruff: noqa: E501
"""Answers to the adverse review of #365 (EF-LEC-005, ENF-SEC-004): the CSS that comes out is read
by a second, independent tokenizer (CSS Syntax 3) and must hold no request the page can make."""

from __future__ import annotations

import html
import re
from collections.abc import Iterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shelfhold_sdk.testing.corpus.trapped_epubs import epub_bytes, xhtml

from shelfhold.infrastructure.content_trust import NeutralizationError, neutralize
from shelfhold.infrastructure.xml import safe_parse_xml

pytestmark = pytest.mark.req("EF-LEC-005", "ENF-SEC-004")

SVG = "http://www.w3.org/2000/svg"
_NEWLINES = "\n\r\f"
_FUNCTIONS = {"url", "image-set", "-webkit-image-set", "image", "cross-fade", "element", "src"}


def _unescape(text: str) -> str:
    def one(m: re.Match[str]) -> str:
        if m.group(1):
            code = int(m.group(1), 16)
            return chr(code) if 0 < code < 0x110000 else "\ufffd"
        return m.group(2)

    return re.sub(r"\\(?:([0-9a-fA-F]{1,6})\s?|([^\n\r\f0-9a-fA-F]))", one, text)


def tokens(css: str) -> Iterator[tuple[str, str]]:
    """(kind, value): `function`, `url`, `at`, `string`, `bad-string`, `ident`; CSS Syntax 3."""
    i, n = 0, len(css)
    while i < n:
        ch = css[i]
        if css.startswith("/*", i):
            end = css.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch in "\"'":
            j = i + 1
            while j < n and css[j] != ch and css[j] not in _NEWLINES:
                j += 2 if css[j] == "\\" else 1
            if j >= n or css[j] in _NEWLINES:
                yield "bad-string", css[i + 1 : j]
                i = j
            else:
                yield "string", css[i + 1 : j]
                i = j + 1
        elif ch == "@":
            m = re.match(r"@((?:[\w-]|\\.)+)", css[i:])
            if m:
                yield "at", _unescape(m.group(1)).lower()
                i += m.end()
            else:
                i += 1
        elif ch.isalpha() or ch in "-_\\" or ord(ch) > 127:
            m = re.match(r"(?:[\w-]|\\[0-9a-fA-F]{1,6}\s?|\\.|[^\x00-\x7f])+", css[i:])
            assert m
            name, i = _unescape(m.group(0)).lower(), i + m.end()
            if i < n and css[i] == "(":
                yield ("url" if name == "url" else "function"), name
                i += 1
            else:
                yield "ident", name
        else:
            i += 1


def requests(css: str) -> list[str]:
    """Everything in `css` that is, or could be read as, a request that is not a fragment."""
    found: list[str] = []
    toks = list(tokens(css))
    for kind, value in toks:
        if kind == "bad-string":
            found.append("bad-string")
        elif kind == "at" and value in {"import", "namespace"}:
            found.append(f"@{value}")
        elif kind == "function" and value in _FUNCTIONS:
            found.append(f"{value}(")
    for m in re.finditer(r"url\s*\(\s*([^)]*)\)", _unescape(css), re.I):
        target = m.group(1).strip("'\" \t\r\n")
        if (
            target
            and not target.startswith("#")
            and (re.match(r"[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith(("/", "\\")))
        ):
            found.append(f"url({target[:30]})")
    return found


def css_of_output(out: str) -> list[str]:
    """Every CSS text of a neutralized page: `style` elements and attributes."""
    root = safe_parse_xml(out.encode())
    texts: list[str] = []
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.rsplit("}", 1)[-1] == "style" and el.text:
            texts.append(el.text)
        for key, value in el.attrib.items():
            if key.rsplit("}", 1)[-1] in ("style", "fill", "stroke", "mask", "clip-path"):
                texts.append(value)
    return texts


def page(body: str) -> str:
    return neutralize(xhtml(body), "xhtml").decode()


HOSTILE = [
    "<p style=\"content:&quot;a&#10;;background-image:image-set('/api/session' 1x);x:&quot;\">t</p>",
    "<p style=\"content:&quot;a&#13;;background:image-set('//e.example/b.png' 1x);x:&quot;\">t</p>",
    "<p style=\"content:&quot;a&#10;;background:image('//e.example/b.png');x:&quot;\">t</p>",
    "<style>p{content:\"a\n;background-image:image-set('//e.example/b.png' 1x);x:\"}\n</style><p>t</p>",
    "<style>p{content:'a\\\n;background:cross-fade(url(//e.example/b.png),red)}</style><p>t</p>",
    "<style>p{content:\"a\n}\n@import '//e.example/a.css';p{x:\"}</style><p>t</p>",
    "<style>p{background:element(#x);b:src('//e.example/')}</style><p>t</p>",
    "<style>p{background:ur/**/l(//e.example/b.png)}</style><p>t</p>",
    "<style>p{background:u\\72l(//e.example/b.png)}</style><p>t</p>",
    '<p style="background:u&#92;72l(//e.example/b.png)">t</p>',
]


@pytest.mark.parametrize("body", HOSTILE)
def test_second_tokenizer_finds_no_request_in_the_output(body: str) -> None:
    out = page(body)
    for css in css_of_output(out):
        assert requests(css) == [], (css, out)
    assert "e.example" not in out and "/api/session" not in out


@settings(max_examples=150, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            [
                "a",
                "\n",
                "\r",
                "\f",
                '"',
                "'",
                ";",
                "{",
                "}",
                "\\",
                "/**/",
                " ",
                ":",
                "(",
                ")",
                "image-set(",
                "url(",
                "@import ",
                "'//e.example/x' ",
                "image(",
                "src(",
                "u\\72l(",
                "x",
            ]
        ),
        min_size=1,
        max_size=14,
    )
)
def test_whatever_the_css_the_output_holds_no_request_and_no_bad_string(parts: list[str]) -> None:
    css = "p{" + "".join(parts) + "}"
    try:
        out = neutralize(css.encode(), "css").decode()
        attr = neutralize(
            xhtml(f'<p style="{html.escape("".join(parts))}">t</p>'), "xhtml"
        ).decode()
        sheet = neutralize(xhtml(f"<style>{html.escape(css)}</style><p>t</p>"), "xhtml").decode()
    except NeutralizationError:
        return
    for text in [out, *css_of_output(attr), *css_of_output(sheet)]:
        assert requests(text) == [], (css, text)


def test_a_string_never_comes_out_with_a_raw_newline() -> None:
    out = neutralize(b"a{content:'x\\\ny'} b{content:\"ok\"} c{content:'x\ny'}", "css").decode()
    assert "ok" in out
    for kind, value in tokens(out):
        assert kind != "bad-string"
        if kind == "string":
            assert not any(c in value for c in _NEWLINES)


def test_a_newline_between_declarations_is_kept() -> None:
    out = neutralize(b"a{color:red;\n margin:0}\n", "css").decode()
    assert "color:red" in out and "margin:0" in out


# --- SVG presentation attributes --------------------------------------------------------------


@pytest.mark.parametrize("attr", ["fill", "stroke", "mask", "clip-path"])
@pytest.mark.parametrize(
    "value",
    ["u\\72l(//e.example/p.svg#p)", "url(\\2f\\2f e.example/p.svg#p)", "u\\72l(#ok)", "red\\"],
)
def test_a_css_escape_in_a_presentation_attribute_removes_the_attribute(
    attr: str, value: str
) -> None:
    out = page(f'<svg xmlns="{SVG}"><rect width="9" height="9" {attr}="{value}"/></svg>')
    assert attr + "=" not in out and "e.example" not in out


@pytest.mark.parametrize(
    "value",
    [
        "image-set('/api/x' 1x)",
        "url(//e.example/p.svg#p)",
        "url(/api/m.svg#m)",
        "url('//e.example/p')",
        "ur/**/l(x)y",
    ],
)
def test_a_presentation_attribute_never_names_a_request(value: str) -> None:
    out = page(
        f'<svg xmlns="{SVG}"><rect width="9" height="9" fill="{value}" mask="{value}"/></svg>'
    )
    assert "e.example" not in out and "/api/" not in out and "image-set" not in out


def test_a_fragment_paint_and_plain_values_stay() -> None:
    out = page(
        f'<svg xmlns="{SVG}"><rect width="9" height="9" fill="url(#g)" stroke="red" id="a"/></svg>'
    )
    assert 'fill="url(#g)"' in out and 'stroke="red"' in out and 'id="a"' in out


# --- a link or a meta in the body ---------------------------------------------------------------


@pytest.mark.parametrize(
    "element",
    [
        '<link rel="icon" href="a.png"/>',
        '<link rel="stylesheet" href="https://e.example/a.css"/>',
        '<link rel="prefetch" href="a.xhtml"/>',
        '<meta name="x" content="y"/>',
        '<meta charset="utf-8"/>',
    ],
)
def test_a_link_or_meta_in_the_body_is_inert_not_a_refusal(element: str) -> None:
    out = neutralize(xhtml(f"<p>a</p>{element}<p>b</p>"), "xhtml")
    body = next(c for c in safe_parse_xml(out) if str(c.tag).endswith("body"))
    assert [str(c.tag).rsplit("}", 1)[-1] for c in body] == ["p", "span", "p"]


def test_a_style_sheet_link_in_the_body_is_kept_when_it_is_a_book_entry() -> None:
    out = neutralize(xhtml('<p>a</p><link rel="stylesheet" href="s.css"/><p>b</p>'), "xhtml")
    body = next(c for c in safe_parse_xml(out) if str(c.tag).endswith("body"))
    assert [str(c.tag).rsplit("}", 1)[-1] for c in body] == ["p", "link", "p"]


def test_an_epub_with_a_body_link_is_still_exported() -> None:
    import io

    from shelfhold.infrastructure.content_trust import neutralize_epub

    book = epub_bytes(xhtml('<p>a</p><link rel="icon" href="a.png"/><meta name="x" content="y"/>'))
    neutralize_epub(io.BytesIO(book), io.BytesIO())  # no refusal
