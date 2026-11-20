"""Answers to the adverse review of `xhtml-neutralizer@v2` (EF-LEC-005, ENF-SEC-004):
style sheet links, emitted CSS, and elements off the allowlist that keep their rank."""

from __future__ import annotations

import io
import zipfile

import pytest
from shelfhold_sdk.testing.corpus.trapped_epubs import (
    Member,
    epub_bytes,
    find_active_css,
    xhtml,
)

from shelfhold.infrastructure.content_trust import neutralize, neutralize_epub
from shelfhold.infrastructure.xml import Element, safe_parse_xml

pytestmark = pytest.mark.req("EF-LEC-005", "ENF-SEC-004")

XHTML = "http://www.w3.org/1999/xhtml"
EVIL = (
    b"<sheet>@import url('https://e.example/a.css'); "
    b"p { background: url(https://e.example/p.png) }</sheet>"
)
LINK = '<link rel="stylesheet" href="{}"/>'


def _local(el: Element) -> str:
    return str(el.tag).rsplit("}", 1)[-1]


def _derive(book: bytes) -> dict[str, bytes]:
    out = io.BytesIO()
    neutralize_epub(io.BytesIO(book), out)
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
        return {n: archive.read(n) for n in archive.namelist()}


def _head_links(page: bytes) -> list[str]:
    head = next(c for c in safe_parse_xml(page) if _local(c) == "head")
    return [c.attrib["href"] for c in head if _local(c) == "link"]


def _book(href: str, *, name: str, media: str, data: bytes = EVIL, body: str = "<p>a</p>") -> bytes:
    return epub_bytes(
        xhtml(body, head=LINK.format(href)),
        extra=[Member(f"OEBPS/{name}", data)],
        opf_extra=f'<item id="x" href="{name}" media-type="{media}"/>',
    )


@pytest.mark.parametrize(
    "href", ["evil.xml", "./evil.xml", "sub/../evil.xml", "evil.xml?v=1#a", "ev%69l.xml"]
)
def test_a_sheet_link_by_any_path_reaches_the_css_neutralizer(href: str) -> None:
    files = _derive(_book(href, name="evil.xml", media="application/xml"))
    assert find_active_css(files["OEBPS/evil.xml"]) == []
    assert b"e.example" not in files["OEBPS/evil.xml"]


def test_the_media_type_does_not_decide() -> None:
    files = _derive(_book("evil.xml", name="evil.xml", media="text/xml"))
    assert b"e.example" not in files["OEBPS/evil.xml"]


def test_a_sheet_link_to_an_entry_with_another_use_is_replaced_by_an_inert_element() -> None:
    """An NCX, a page, an entry named by another reference: it cannot be a style sheet."""
    body = '<p>a</p><link rel="stylesheet" href="toc.ncx"/><p>b</p>'
    book = epub_bytes(
        xhtml(body, head=LINK.format("toc.ncx")),
        extra=[Member("OEBPS/toc.ncx", EVIL)],
        opf_extra='<item id="n" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
    )
    files = _derive(book)
    page = files["OEBPS/ch1.xhtml"]
    assert _head_links(page) == []  # the head keeps nothing
    body_el = next(c for c in safe_parse_xml(page) if _local(c) == "body")
    assert [_local(c) for c in body_el] == ["p", "span", "p"]  # the rank is kept in the body


def test_a_xml_entry_that_a_page_also_names_otherwise_is_not_a_style_sheet() -> None:
    body = '<p><a href="shared.xml">x</a></p>'
    files = _derive(_book("shared.xml", name="shared.xml", media="application/xml", body=body))
    assert _head_links(files["OEBPS/ch1.xhtml"]) == []
    assert files["OEBPS/shared.xml"] == EVIL  # still read as XML, never as a style sheet


def test_an_image_named_as_a_style_sheet_loses_the_link() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"@import url(https://e.example/a.css);"
    files = _derive(_book("pic.png", name="pic.png", media="image/png", data=png))
    assert _head_links(files["OEBPS/ch1.xhtml"]) == []


def test_a_style_sheet_by_name_is_still_neutralized() -> None:
    files = _derive(
        _book(
            "s.css",
            name="s.css",
            media="text/css",
            data=b"@import 'https://e.example/a.css'; a{color:red}",
        )
    )
    assert b"e.example" not in files["OEBPS/s.css"] and b"color:red" in files["OEBPS/s.css"]
    assert _head_links(files["OEBPS/ch1.xhtml"]) == ["s.css"]


# --- the CSS that comes out is the CSS that was analyzed -----------------------------------------


def test_a_comment_never_reaches_the_output() -> None:
    out = neutralize(
        b"/* secret ur/**/l(x) */ a { color: red /* c */ } "
        b"b { background: ur/**/l(https://e.example/p) }",
        "css",
    )
    assert b"/*" not in out and b"e.example" not in out and b"color: red" in out


def test_a_style_attribute_is_emitted_without_its_comments() -> None:
    out = neutralize(xhtml('<p style="color:red/* c */;margin:0">a</p>'), "xhtml")
    assert b"/*" not in out and b"color:red" in out


# --- an element off the allowlist keeps its rank and its children --------------------------------


def test_an_unknown_element_becomes_a_wrapper_that_keeps_its_children() -> None:
    document = xhtml("<p>a</p><font>x<b>y</b></font><center><p>c</p></center><p>d</p>")
    body = next(c for c in safe_parse_xml(neutralize(document, "xhtml")) if _local(c) == "body")
    assert [_local(c) for c in body] == ["p", "span", "div", "p"]
    assert body[1].text == "x" and [_local(c) for c in body[1]] == ["b"]
    assert [_local(c) for c in body[2]] == ["p"]


def test_an_unknown_element_in_a_table_is_a_row_wrapper() -> None:
    out = neutralize(xhtml("<table><font><tr><td>x</td></tr></font></table>"), "xhtml")
    table = next(e for e in safe_parse_xml(out).iter() if _local(e) == "table")
    assert [_local(c) for c in table] == ["tr"]


def test_a_foreign_namespace_element_keeps_its_children() -> None:
    document = xhtml('<p>a</p><x:note xmlns:x="urn:x"><p>n</p></x:note><p>b</p>')
    body = next(c for c in safe_parse_xml(neutralize(document, "xhtml")) if _local(c) == "body")
    assert [_local(c) for c in body] == ["p", "span", "p"]
