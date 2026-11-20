"""Adverse review of `xhtml-neutralizer@v2` (second independent opinion, nothing fixed here).

Each `xfail(strict=True)` test is the proof of a hole found by reading the lot and attacking the
filter as a hostile EPUB; red on purpose until the hole is closed. The detector used by the
assertions is the corpus one (`trapped_epubs.detect`), which shares nothing with the
neutralizer: the invariant it documents is "a neutralized document must give an empty list".

* R1 (high, pipeline): a page may link a style sheet by any relative path, but only `.css`
  entries are neutralized as style sheets. An `.xml`/`.ncx` entry (the OPF itself works too) is
  copied verbatim after XML checks that never look at its text, so a hostile book ships `@import`
  and a remote `url()` to the reader through an entry the page loads as a style sheet: the
  derivative holds remote CSS although "nothing remote comes out" is the invariant of the lot.
  The client filter keeps `link rel="stylesheet"` to a book entry (ADR-0055) and a `blob:` style
  sheet is applied whatever its type (no `nosniff` on a blob), so the client does not close it.
* R2 (low, XHTML): a `style=` attribute is emitted verbatim although its safety was decided on a
  comment-stripped copy, so `ur/**/l(https://…)` survives: to a browser a comment is a token
  separator (this is a function `l(…)`, not `url(…)`), but the emitted bytes are not the analyzed
  bytes and the corpus detector flags them.
* R3 (low, CSS): the same in a style sheet file: `@/**/import 'https://…';` is emitted verbatim
  (inert to a browser: `@` and `import` are two tokens) and flagged by the detector.
* R4 (low, SVG): a small raster `data:image` URL on an SVG `<image>` is allowed by the ADR
  ("raster `data:image` URLs on images") but the shipped detector exempts `<img>` only, so the
  neutralizer's own output breaks the detector invariant.

The last two tests are the vectors of this review that the filter already holds, kept here as
regression proof of the second opinion.
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest
from shelfhold_sdk.testing.corpus.trapped_epubs import (
    Member,
    epub_bytes,
    find_active_css,
    find_active_patterns,
    xhtml,
)

from shelfhold.infrastructure.content_trust import NeutralizationError, neutralize, neutralize_epub

pytestmark = pytest.mark.req("EF-LEC-005", "ENF-SEC-004")

SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"

EVIL_SHEET = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b"<sheet>@import url('https://e.example/a.css');\n"
    b'p { background: url("https://e.example/p.png") }\n'
    b"</sheet>\n"
)


def test_a_style_sheet_link_to_a_verbatim_xml_entry_ships_remote_css() -> None:
    """`link rel="stylesheet" href="evil.xml"`: the page is clean, the entry it loads is not."""
    book = epub_bytes(
        xhtml("<p>a</p>", head='<link rel="stylesheet" href="evil.xml"/>'),
        extra=[Member("OEBPS/evil.xml", EVIL_SHEET)],
        opf_extra='<item id="evil" href="evil.xml" media-type="application/xml"/>',
    )
    out = io.BytesIO()
    neutralize_epub(io.BytesIO(book), out)
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
        page = archive.read("OEBPS/ch1.xhtml").decode()
        sheet = archive.read("OEBPS/evil.xml")
    assert '<link rel="stylesheet" href="evil.xml"/>' in page  # the link survives, by design
    assert find_active_css(sheet) == []  # … so the entry it names must be a clean style sheet


def test_a_style_attribute_is_emitted_as_the_text_that_was_analyzed() -> None:
    """`ur/**/l(…)`: the comment hides the keyword from the check, not from the output."""
    document = xhtml('<p style="background:ur/**/l(https://e.example/p.png)">a</p>')
    assert find_active_patterns(neutralize(document, "xhtml")) == []


def test_a_style_sheet_is_emitted_as_the_text_that_was_analyzed() -> None:
    """`@/**/import 'https://…';`: kept verbatim, flagged by the corpus detector."""
    sheet = b"@/**/import 'https://e.example/a.css'; a { color: red }"
    assert find_active_css(neutralize(sheet, "css")) == []


def test_a_raster_data_url_on_an_svg_image_is_clean_output() -> None:
    """The ADR allows small raster `data:image` URLs on images; the output must be empty."""
    svg = (
        f'<svg xmlns="{SVG}" xmlns:xlink="{XLINK}">'
        '<image xlink:href="data:image/png;base64,iVBORw0KGgo="/><rect width="1" height="1"/>'
        "</svg>"
    ).encode()
    assert find_active_patterns(neutralize(svg, "svg")) == []


# --- what the review found holding, kept as regression proof -------------------------------------

REMOVED = {
    "script": "<script>alert(1)</script>",
    "iframe-srcdoc": '<iframe srcdoc="&lt;script&gt;1&lt;/script&gt;"></iframe>',
    "object": '<object data="a.html" type="text/html"><p>fb</p></object>',
    "embed": '<embed src="a.swf"/>',
    "meta-refresh": '<meta http-equiv="refresh" content="0;url=https://e.example/"/>',
    "base": '<base href="https://e.example/"/>',
    "link-import": '<link rel="import" href="https://e.example/x.html"/>',
    "svg-set": (
        f'<svg xmlns="{SVG}"><a href="#">'
        '<set attributeName="href" to="javascript:alert(1)"/><text>x</text></a></svg>'
    ),
    "svg-foreign": f'<svg xmlns="{SVG}"><foreignObject><script>1</script></foreignObject></svg>',
    "math": '<math xmlns="http://www.w3.org/1998/Math/MathML"><maction><mi>a</mi></maction></math>',
}


@pytest.mark.parametrize("name", sorted(REMOVED))
def test_the_removed_element_of_the_review_corpus_is_neutralized(name: str) -> None:
    """The vectors of this review that the filter already holds: nothing active comes out."""
    document = xhtml(f"<p>a</p>{REMOVED[name]}<p>b</p>")
    try:
        out = neutralize(document, "xhtml")
    except NeutralizationError:  # a refusal is a valid answer: nothing is served
        return
    assert find_active_patterns(out) == [], out
    text = out.decode("utf-8")
    assert re.search(r"\son\w+=", text) is None
    assert "e.example" not in text and "srcdoc" not in text


def test_an_xxe_and_a_billion_laughs_are_refused() -> None:
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE f [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b"<html><body>&x;</body></html>"
    )
    laughs = (
        b'<?xml version="1.0"?><!DOCTYPE b [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        b"<html><body>&b;</body></html>"
    )
    for data in (xxe, laughs):
        with pytest.raises(NeutralizationError):
            neutralize(data, "xhtml")
