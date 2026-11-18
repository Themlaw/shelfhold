"""xhtml-neutralizer@v2: the one producer of the derivative of an `untrusted` EPUB (EF-BIB-051).

One pure, deterministic function, `neutralize(data, kind)`, over one XHTML page, one SVG image or
one style sheet. The same bytes always give the same bytes; nothing is read from the clock, the
environment or the network. A change of behaviour is a new version (`NEUTRALIZER_VERSION`): the
version is part of every derivative's identity (`derived_by`), so a security fix regenerates them.

How it works, and why it is shaped this way.

* The document is first read by `safe_parse_xml` (no DTD, no entity, size, depth and element caps).
  A simple XHTML `<!DOCTYPE ...>` without an internal subset is removed beforehand, and the HTML
  named entities it used to provide (`&nbsp;`) become numeric references; any other DTD is refused.
* The result is not the input with things cut out: it is a **new document** written from the parsed
  tree, element by element, from an allowlist. Comments, processing instructions and CDATA sections
  are never copied (a reader that parses the page as HTML would see them differently from an XML
  parser, which is how `<![CDATA[ > <script>...` hides a script). What is not on the allowlist is
  wrapped in a plain element (its content stays) and what is dangerous is dropped with its whole
  subtree. Element and attribute names are compared in lower case too, since the reader's HTML
  parser does.
* **An element that is removed leaves an inert element at its rank** (since v2, ADR-0055): an
  empty element without attribute (`span`, or the one the parent accepts: `tr` in a table, `td` in
  a row, `col` in a column group, `g` in an SVG). A reading position (an EPUB CFI) counts the
  siblings of a node: with nothing left in place, removing a script would move every position
  after it, from one release to the next. The text around it stays in two separate nodes. The
  head has no placeholder. An element off the allowlist that is not
  dangerous (`font`, `center`, a foreign namespace) is not unwrapped either: it becomes a plain
  `span` (`div` for a block element of old HTML) that keeps its children, so no rank moves.
* URLs are checked on a squashed copy (no control, space or invisible character, lower case, `\\`
  read as `/`), the way a browser's parser would, whatever entities wrote them. Only relative
  references pass, plus small raster `data:image` URLs on images. Anything with a scheme (a link to
  `http`, `https` or `mailto` loses its `href` and keeps its text: the reader may be a child), a
  leading `//` or `/` is dropped. A `xml-stylesheet` processing instruction refuses the page.
* Style sheets (`<style>`, `style=` and `.css` files) are cut rule by rule: a rule survives only
  without `@import`, escapes, `expression()`, `behavior`, `-moz-binding`, `image-set()` and any
  `url()` that is not a relative reference.
* nh3 is the second, independent gate. The body of the new document is cleaned by nh3 with the
  same allowlist, and both are read back as start tags and attributes: if nh3 would have changed
  anything, the HTML parser and this walk disagree about the page, and the page is refused
  (`invariant`) instead of exported. This module is the only one that gives XHTML to nh3
  (`epub-derivative-single-producer`); the profile of `sanitize_upstream_text` does not fit pages.
"""

from __future__ import annotations

import html.entities
import posixpath
import re
import unicodedata
from html.parser import HTMLParser
from typing import Final, Literal
from urllib.parse import unquote

import nh3

from shelfhold.infrastructure.content_trust.cbz_entries import UNTRUSTED_ENTRY_CODE
from shelfhold.infrastructure.xml import Element, UnsafeXmlError, XmlLimits, safe_parse_xml

Kind = Literal["xhtml", "svg", "css"]

NEUTRALIZER_VERSION: Final = 2
DERIVED_BY: Final = f"xhtml-neutralizer@v{NEUTRALIZER_VERSION}"
# The `filter` field of a stored reading position: the version of the filter and of this
# neutralizer that produced the DOM the position was written on (ADR-0055, locator v1).
CONTENT_FILTER: Final = f"content-filter@{NEUTRALIZER_VERSION}"

XHTML_NS: Final = "http://www.w3.org/1999/xhtml"
SVG_NS: Final = "http://www.w3.org/2000/svg"
XLINK_NS: Final = "http://www.w3.org/1999/xlink"
EPUB_NS: Final = "http://www.idpf.org/2007/ops"
XML_NS: Final = "http://www.w3.org/XML/1998/namespace"

XML_LIMITS: Final = XmlLimits(max_bytes=16 * 1024 * 1024)
_PROLOG: Final = '<?xml version="1.0" encoding="utf-8"?>\n'


class NeutralizationError(ValueError):
    """The document cannot be neutralized, so the whole EPUB is refused (nothing is exported)."""

    code = UNTRUSTED_ENTRY_CODE

    def __init__(self, reason: str, entry: str | None = None) -> None:
        super().__init__(f"{UNTRUSTED_ENTRY_CODE}: {reason}" + (f" ({entry})" if entry else ""))
        self.reason = reason
        self.entry = entry


# --- Allowlists ----------------------------------------------------------------------------------


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_VOID: Final = frozenset({"br", "hr", "img", "wbr", "col", "link", "meta"})
_XHTML_TAGS: Final = _words(
    """
    a abbr address article aside b bdi bdo blockquote br caption cite code col colgroup dd del
    details dfn div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd
    li main mark nav ol p pre q rb rp rt rtc ruby s samp section small span strong sub summary
    sup table tbody td tfoot th thead time tr u ul var wbr link meta style
    """
)
_SVG_TAGS: Final = _words(
    """
    svg g defs symbol use path rect circle ellipse line polyline polygon text tspan title desc
    linearGradient radialGradient stop clipPath mask pattern image style
    """
)
# Dropped with their whole subtree, whatever the namespace or the letter case.
_DANGEROUS: Final = _words(
    """
    script iframe frame frameset object embed applet form input button select textarea option
    optgroup datalist base foreignobject animate animatemotion animatetransform set discard
    handler listener video audio source track picture canvas math noscript template portal
    fencedframe xmp plaintext noembed noframes dialog param keygen menu command
    """
)
_GLOBAL_ATTRS: Final = _words(
    """
    id class lang dir title role aria-label aria-hidden aria-labelledby aria-describedby
    aria-level aria-roledescription style
    """
)
_XHTML_ATTRS: Final[dict[str, frozenset[str]]] = {
    "a": frozenset({"href"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "td": frozenset({"colspan", "rowspan", "headers", "scope", "abbr"}),
    "th": frozenset({"colspan", "rowspan", "headers", "scope", "abbr"}),
    "ol": frozenset({"start", "reversed", "type"}),
    "li": frozenset({"value"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "time": frozenset({"datetime"}),
    "details": frozenset({"open"}),
    "link": frozenset({"rel", "type", "href", "media"}),
    "meta": frozenset({"charset", "name", "content"}),
    "style": frozenset({"type", "media"}),
}
_SVG_ATTRS: Final = (
    _words(
        """
        viewBox width height x y x1 y1 x2 y2 cx cy r rx ry fx fy d points transform fill stroke
        stroke-width stroke-linecap stroke-linejoin stroke-dasharray stroke-dashoffset
        stroke-opacity stroke-miterlimit fill-opacity fill-rule clip-rule opacity offset stop-color
        stop-opacity gradientUnits gradientTransform spreadMethod patternUnits patternTransform
        patternContentUnits clipPathUnits maskUnits maskContentUnits preserveAspectRatio font-family
        font-size font-weight font-style text-anchor dominant-baseline dx dy rotate textLength
        lengthAdjust letter-spacing word-spacing text-decoration visibility display clip-path mask
        version href media type
        """
    )
    | _GLOBAL_ATTRS
)
_URL_PAINT: Final = re.compile(r"^\s*(?:none|currentcolor|[#\w(),.\s%-]*)$", re.I)

_SCHEME: Final = re.compile(r"^[a-z][a-z0-9+.\-]*:")
_DATA_IMAGE: Final = re.compile(r"^data:image/(?:png|jpeg|gif|webp|avif);base64,[a-z0-9+/=]*$")
_INVISIBLE: Final = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp", "Co", "Cn", "Cs"})


# --- URL checks ----------------------------------------------------------------------------------


def _squash(value: str) -> str:
    """The URL as a browser's parser reads it: nothing invisible, `\\` as `/`, lower case."""
    kept = (ch for ch in value if unicodedata.category(ch) not in _INVISIBLE)
    return "".join(kept).replace("\\", "/").lower()


def _is_relative(value: str) -> bool:
    """A reference inside the package: no scheme, no authority, not rooted at the host."""
    s = _squash(value)
    return bool(s) and not s.startswith("/") and not _SCHEME.match(s)


def is_relative_reference(value: str) -> bool:
    """True for a reference inside the package (what the EPUB scan accepts in an OPF)."""
    return _is_relative(value)


def _is_fragment(value: str) -> bool:
    return _squash(value).startswith("#")


def _image_ok(value: str) -> bool:
    return _is_relative(value) or bool(_DATA_IMAGE.match(_squash(value)))


# --- CSS -----------------------------------------------------------------------------------------

_CSS_BAD: Final = re.compile(
    r"@import|expression\s*\(|(?:behavior|behaviour)\s*:|-moz-binding|javascript:|vbscript:|livescript:"
    r"|image-set\s*\(|(?:src|image|cross-fade|element|paint)\s*\(|@namespace",
    re.I,
)
_CSS_URL_CALL: Final = re.compile(r"url\s*\(", re.I)
_CSS_URL_FULL: Final = re.compile(r"""url\s*\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s"']*))\s*\)""", re.I)


def _css_split(text: str, sep: str = " ") -> tuple[str, str] | None:
    """(text without comments, the same with every string emptied), or None if it is not closed.

    A comment becomes `sep`: a space (what a browser's tokenizer sees) or nothing (what a naive
    reader sees, `ur/**/l(` read as `url(`).
    """
    out: list[str] = []
    bare: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                return None
            out.append(sep)
            bare.append(sep)
            i = end + 2
        elif ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                return None
            out.append(text[i : j + 1])
            bare.append(ch + ch)
            i = j + 1
        else:
            out.append(ch)
            bare.append(ch)
            i += 1
    return "".join(out), "".join(bare)


def _css_checks(text: str, bare: str) -> bool:
    """True if the comment-free `text` (and `bare`, its strings emptied) must not survive."""
    if "\\" in bare or _CSS_BAD.search(bare):
        return True
    calls = len(_CSS_URL_CALL.findall(bare))
    if calls != len(_CSS_URL_CALL.findall(text)):  # a `url(` hidden in a string is still a flag
        return True
    for match in _CSS_URL_FULL.finditer(text):
        target = next(g for g in match.groups() if g is not None)
        if not (_is_relative(target) or _is_fragment(target)):
            return True
    return len(_CSS_URL_FULL.findall(text)) != calls


def _css_text(chunk: str) -> str | None:
    """The rule as it is written out (comments gone), or None if it must not survive.

    What is emitted is the analyzed text, never the bytes it was cut from, and it is analyzed
    twice: a comment read as a space and as nothing (`@/**/import`, `ur/**/l(`).
    """
    if any(ch in "<&" for ch in chunk) or "]]>" in chunk:
        return None
    if any(unicodedata.category(ch) == "Cc" and ch not in "\t\n\r\f" for ch in chunk):
        return None
    spaced, joined = _css_split(chunk, " "), _css_split(chunk, "")
    if spaced is None or joined is None:
        return None
    if _css_checks(*spaced) or _css_checks(*joined):
        return None
    return spaced[0]


def _css_chunks(text: str) -> list[str]:
    """Top-level statements and blocks, cut where a browser's parser would, verbatim."""
    chunks: list[str] = []
    start = depth = i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)
            if depth == 0:
                chunks.append(text[start : i + 1])
                start = i + 1
        elif ch == ";" and depth == 0:
            chunks.append(text[start : i + 1])
            start = i + 1
        i += 1
    if start < n:
        chunks.append(text[start:])
    return chunks


def _clean_sheet(text: str) -> str:
    """The style sheet without the rules that fetch, import or run something, as analyzed."""
    out: list[str] = []
    for chunk in _css_chunks(text):
        if not chunk.strip():
            out.append(chunk)
        elif (kept := _css_text(chunk)) is not None:
            out.append(kept)
    return "".join(out)


def _clean_style_attr(value: str) -> str | None:
    return _css_text(value)


# --- Writer --------------------------------------------------------------------------------------


def _esc_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(text: str) -> str:
    return (
        _esc_text(text).replace('"', "&quot;").replace("\t", "&#9;").replace("\n", "&#10;")
        .replace("\r", "&#13;")
    )  # fmt: skip


def _split(name: str) -> tuple[str | None, str]:
    if name.startswith("{"):
        ns, _, local = name[1:].partition("}")
        return ns, local
    return None, name


def _resolve(base: str, reference: str) -> str:
    """The package path a relative reference names ("" if it leaves the package)."""
    target = unquote(reference.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
    if not target:
        return ""
    path = posixpath.normpath(posixpath.join(base, target))
    return "" if path == ".." or path.startswith(("../", "/")) else path


_TABLE_PARENTS: Final = frozenset({"table", "tbody", "thead", "tfoot"})
_BLOCK_UNKNOWN: Final = frozenset({"center", "dir", "listing", "marquee", "multicol"})


def _wrapper_tag(context: str, parent: str, name: str) -> str:
    """The tag that stands for an element off the allowlist, one the parent accepts."""
    if context == "svg":
        return "g"
    if parent in _TABLE_PARENTS:
        return "tr"
    if parent == "tr":
        return "td"
    if parent == "colgroup":
        return "col"
    return "div" if name in _BLOCK_UNKNOWN else "span"


def _inert(context: str, parent: str) -> str:
    """What stands where an element was removed: empty, no attribute, accepted by its parent.

    A `span` inside a table would be moved by an HTML parser, so the rank would not be kept.
    Nothing in the head (`parent == "head"`): no reading position goes there.
    """
    if parent == "head":
        return ""
    if context == "svg":
        return "<g/>"
    if parent in _TABLE_PARENTS:
        return "<tr></tr>"
    if parent == "tr":
        return "<td></td>"
    if parent == "colgroup":
        return "<col/>"
    return "<span></span>"


class _Writer:
    """Walks the parsed tree and writes the allowlisted subset as a new document."""

    def __init__(self, path: str = "", blocked: frozenset[str] = frozenset()) -> None:
        self._dir = posixpath.dirname(path)
        self._blocked = blocked  # package paths a style sheet link must not name (R1)

    def _attributes(self, el: Element, local: str, context: str) -> list[tuple[str, str]]:
        allowed = _SVG_ATTRS if context == "svg" else _GLOBAL_ATTRS | _XHTML_ATTRS.get(local, set())
        out: list[tuple[str, str]] = []
        for key, value in el.attrib.items():
            ns, name = _split(key)
            low = name.lower()
            if low.startswith("on"):
                continue
            if ns is None:
                if name not in allowed or (context == "xhtml" and name != low):
                    continue
                if name == "style":
                    value = _clean_style_attr(value)  # type: ignore[assignment]
                    if value is None:
                        continue
                elif name in ("href", "src"):
                    if not self._url_attr(local, name, value, context):
                        continue
                elif context == "svg" and "url" in _squash(value):
                    if not _URL_PAINT.match(value) or not self._fragment_urls(value):
                        continue
                qname = name
            elif ns == XML_NS and name in ("lang", "space"):
                qname = f"xml:{name}"
            elif ns == EPUB_NS and name in ("type", "prefix"):
                qname = f"epub:{name}"
            elif ns == XLINK_NS and name == "href" and local in ("image", "use"):
                if not self._url_attr(local, name, value, context):
                    continue
                qname = "xlink:href"
            else:
                continue
            out.append((qname, value))
        return out

    @staticmethod
    def _fragment_urls(value: str) -> bool:
        return all(
            m.group(1).lstrip().startswith("#") for m in re.finditer(r"url\(([^)]*)\)", value, re.I)
        )

    @staticmethod
    def _url_attr(local: str, name: str, value: str, context: str) -> bool:
        if context == "xhtml":
            if local == "a":
                return _is_relative(value)
            if local == "img":
                return _image_ok(value)
            if local == "link":
                return _is_relative(value)
            return False
        if local == "use":
            return _is_fragment(value)
        if local == "image":
            return _image_ok(value)
        return False

    @staticmethod
    def _start(qname: str, attrs: list[tuple[str, str]], extra: str = "") -> str:
        decl = ""
        if any(k.startswith("epub:") for k, _ in attrs):
            decl += f' xmlns:epub="{EPUB_NS}"'
        if any(k.startswith("xlink:") for k, _ in attrs):
            decl += f' xmlns:xlink="{XLINK_NS}"'
        body = "".join(f' {k}="{_esc_attr(v)}"' for k, v in attrs)
        return f"<{qname}{extra}{decl}{body}"

    def children(self, el: Element, context: str) -> str:
        parent = _split(el.tag)[1].lower() if isinstance(el.tag, str) else ""
        parts = [_esc_text(el.text or "")]
        for child in el:
            parts.append(self.element(child, context, parent))
            parts.append(_esc_text(child.tail or ""))
        return "".join(parts)

    def element(self, el: Element, context: str, parent: str = "") -> str:
        """The element as written, or the inert element that holds its rank."""
        if not isinstance(el.tag, str):  # a comment or an instruction is not a sibling
            return ""
        return self._element(el, context, parent) or _inert(context, parent)

    def _element(self, el: Element, context: str, parent: str = "") -> str:
        ns, local = _split(el.tag)
        low = local.lower()
        if low in _DANGEROUS:
            return ""
        if ns == SVG_NS or (ns is None and context == "xhtml" and local == "svg"):
            context = "svg"
        elif ns not in (XHTML_NS, None):
            return self._wrap(el, context, parent, low)
        if context == "svg":
            return self._svg(el, local, parent)
        if local not in _XHTML_TAGS:
            return self._wrap(el, context, parent, low)
        if local == "style":
            return self._style(el, local, context)
        if local == "link":
            return self._link(el)
        if local == "meta":
            return self._meta(el)
        attrs = self._attributes(el, local, context)
        if local == "img" and not any(k == "src" for k, _ in attrs):
            return ""
        start = self._start(local, attrs)
        if local in _VOID:
            return start + "/>"
        return f"{start}>{self.children(el, context)}</{local}>"

    def _wrap(self, el: Element, context: str, parent: str, name: str) -> str:
        """An element off the allowlist (not dangerous) keeps its children in a plain wrapper.

        The wrapper takes its rank and the children keep theirs one level down, as they were:
        the CFI steps do not move. `div` for a block element of old HTML, `span` otherwise, what
        the parent accepts in a table, `g` in an SVG.
        """
        tag = _wrapper_tag(context, parent, name)
        if tag == "col":  # a column has no content
            return "<col/>"
        inner = self.children(el, context)
        return f"<{tag}/>" if tag == "g" and not inner else f"<{tag}>{inner}</{tag}>"

    def _svg(self, el: Element, local: str, parent: str = "") -> str:
        if local not in _SVG_TAGS:
            return self._wrap(el, "svg", parent, local.lower())
        if local == "style":
            return self._style(el, local, "svg")
        attrs = self._attributes(el, local, "svg")
        if local in ("use", "image") and not any(k in ("href", "xlink:href") for k, _ in attrs):
            return ""
        _, own = _split(el.tag)
        extra = f' xmlns="{SVG_NS}"' if own == "svg" else ""
        start = self._start(local, attrs, extra)
        inner = self.children(el, "svg")
        return f"{start}/>" if not inner else f"{start}>{inner}</{local}>"

    def _style(self, el: Element, local: str, context: str) -> str:
        attrs = self._attributes(el, local, context)
        if any(k == "type" and v != "text/css" for k, v in attrs):
            return ""
        text = _clean_sheet(el.text or "")
        if not text.strip():
            return ""
        return f"{self._start('style', attrs)}>{text}</style>"

    def _link(self, el: Element) -> str:
        attrs = self._attributes(el, "link", "xhtml")
        rel = dict(attrs).get("rel", "").lower().split()
        if rel != ["stylesheet"] or not any(k == "href" for k, _ in attrs):
            return ""
        if self._blocked and _resolve(self._dir, dict(attrs)["href"]) in self._blocked:
            return ""  # an entry that cannot be a style sheet: its link leaves an inert element
        return self._start("link", attrs) + "/>"

    def _meta(self, el: Element) -> str:
        if any(_split(k)[1].lower() == "http-equiv" for k in el.attrib):
            return ""
        attrs = self._attributes(el, "meta", "xhtml")
        names = {k for k, _ in attrs}
        if names == {"charset"}:  # a browser would honour any other label (utf-7...)
            return self._start("meta", attrs) + "/>" if _charset_ok(dict(attrs)["charset"]) else ""
        if not (names == {"charset"} or names == {"name", "content"}):
            return ""
        return self._start("meta", attrs) + "/>"


_CHARSETS: Final = frozenset({"utf-8", "utf-16"})


def _charset_ok(value: str) -> bool:
    return value.strip().lower() in _CHARSETS


# --- nh3, the second gate ------------------------------------------------------------------------

_NH3_TAGS: Final = (_XHTML_TAGS - {"link", "meta"}) | _SVG_TAGS | {t.lower() for t in _SVG_TAGS}
_NH3_ATTRS: Final = {
    "*": set(_SVG_ATTRS)
    | {a.lower() for a in _SVG_ATTRS}
    | {"xlink:href", "epub:type", "epub:prefix", "xml:lang", "xml:space", "href", "src"}
    | set().union(*_XHTML_ATTRS.values()),
}
_NH3_CONTENT_TAGS: Final = {"script", "iframe", "object", "embed", "noscript", "template"}


class _Shape(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, tuple[tuple[str, str], ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tbody":  # the HTML parser adds one around bare rows: not a difference
            return
        kept = sorted((k, v or "") for k, v in attrs if not k.startswith("xmlns"))
        self.tags.append((tag, tuple(kept)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _shape(fragment: str) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    parser = _Shape()
    parser.feed(fragment)
    parser.close()
    return parser.tags


def _nh3_agrees(fragment: str) -> bool:
    """nh3 (allowlist, HTML5 parser) must have nothing to remove from what was written."""
    cleaned = nh3.clean(
        fragment,
        tags=_NH3_TAGS,
        attributes=_NH3_ATTRS,
        url_schemes={"http", "https", "mailto", "data"},
        link_rel=None,
        strip_comments=True,
        clean_content_tags=_NH3_CONTENT_TAGS,
    )
    return _shape(cleaned) == _shape(fragment)


# --- Entry points --------------------------------------------------------------------------------

_DOCTYPE: Final = re.compile(
    rb"""<!DOCTYPE(?:\s+[^\s>\["']+|\s+"[^"]*"|\s+'[^']*')*\s*>""",
)
_ELEMENT_START: Final = re.compile(rb"<[A-Za-z_:]")
_NAMED_ENTITY: Final = re.compile(rb"&([A-Za-z][A-Za-z0-9]*);")
_XML_ENTITIES: Final = frozenset({"amp", "lt", "gt", "quot", "apos"})


def _without_simple_doctype(data: bytes) -> bytes:
    """Drop a DOCTYPE with no internal subset (XHTML 1.x) and write its HTML entities as numbers.

    Anything else (an internal subset, a parameter entity) stays in the bytes and is refused by
    the XML entry point.
    """
    first = _ELEMENT_START.search(data)
    prolog = data[: first.start()] if first else data
    found = _DOCTYPE.search(prolog)
    if found is None:
        return data
    data = data[: found.start()] + data[found.end() :]

    def number(match: re.Match[bytes]) -> bytes:
        name = match.group(1).decode("ascii")
        if name in _XML_ENTITIES or name not in html.entities.name2codepoint:
            return match.group(0)
        return b"&#%d;" % html.entities.name2codepoint[name]

    return _NAMED_ENTITY.sub(number, data)


_XML_STYLESHEET: Final = re.compile(rb"<\?xml-stylesheet", re.I)


def _parse(data: bytes) -> Element:
    # An XSL processing instruction can run a transform and fetch from the network, and the XML
    # parser drops it silently: look for it in the bytes (UTF-16 included) and refuse.
    if _XML_STYLESHEET.search(data.replace(b"\x00", b"")):
        raise NeutralizationError("xml_stylesheet")
    try:
        return safe_parse_xml(_without_simple_doctype(bytes(data)), limits=XML_LIMITS)
    except UnsafeXmlError as error:
        raise NeutralizationError(f"xml_{error.reason}") from None


def _head(writer: _Writer, root: Element) -> str:
    parts: list[str] = []
    for head in root:
        if _split(head.tag)[1].lower() != "head":
            continue
        for child in head:
            ns, local = _split(child.tag)
            if ns not in (XHTML_NS, None):
                continue
            if local == "title":
                parts.append(f"<title>{_esc_text(''.join(child.itertext()))}</title>")
            elif local in ("meta", "link", "style"):
                parts.append(writer.element(child, "xhtml", "head"))
    return "".join(parts)


XML_LIMITS_INSPECT: Final = XmlLimits(max_bytes=4 * 1024 * 1024)


def parse_xml_document(data: bytes, limits: XmlLimits = XML_LIMITS) -> Element:
    """Read an XML document the way a page is read (a simple DOCTYPE tolerated, nothing else)."""
    try:
        return safe_parse_xml(_without_simple_doctype(bytes(data)), limits=limits)
    except UnsafeXmlError as error:
        raise NeutralizationError(f"xml_{error.reason}") from None


def _xhtml(data: bytes, writer: _Writer) -> bytes:
    root = _parse(data)
    ns, local = _split(root.tag)
    if local.lower() != "html" or ns not in (XHTML_NS, None):
        raise NeutralizationError("root")
    bodies = [b for b in root if _split(b.tag)[1].lower() == "body"]
    inner = "".join(writer.children(b, "xhtml") for b in bodies)
    if not _nh3_agrees(inner):
        raise NeutralizationError("invariant")
    html_start = writer._start(
        "html", writer._attributes(root, "html", "xhtml"), f' xmlns="{XHTML_NS}"'
    )
    body_attrs = writer._attributes(bodies[0], "body", "xhtml") if bodies else []
    body_start = writer._start("body", body_attrs)
    head = _head(writer, root)
    return f"{_PROLOG}{html_start}><head>{head}</head>{body_start}>{inner}</body></html>".encode()


def _svg_document(data: bytes, writer: _Writer) -> bytes:
    root = _parse(data)
    ns, local = _split(root.tag)
    if local != "svg" or ns not in (SVG_NS, None):
        raise NeutralizationError("root")
    written = writer.element(root, "svg")
    if not written or not _nh3_agrees(written):
        raise NeutralizationError("invariant" if written else "root")
    return (_PROLOG + written).encode()


def _css(data: bytes) -> bytes:
    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise NeutralizationError("encoding") from None
    return _clean_sheet(text).encode()


def neutralize(data: bytes, kind: Kind) -> bytes:
    """The neutralized form of one XHTML page, SVG image or style sheet (`xhtml-neutralizer@v2`).

    Pure and deterministic. Raises `NeutralizationError` for a document it refuses to touch
    (unsafe XML, wrong root, a disagreement with nh3): the EPUB it belongs to is then not exported.
    """
    return neutralize_page(data, kind)


def neutralize_page(
    data: bytes, kind: Kind, *, path: str = "", blocked: frozenset[str] = frozenset()
) -> bytes:
    """`neutralize` for a page of a package: `path` is its place in the package and `blocked` the
    entries a `link rel="stylesheet"` must not name (they cannot be neutralized as style sheets)."""
    if kind == "xhtml":
        return _xhtml(data, _Writer(path, blocked))
    if kind == "svg":
        return _svg_document(data, _Writer(path, blocked))
    if kind == "css":
        return _css(data)
    raise ValueError(f"unknown kind {kind!r}")


def page_references(data: bytes, path: str) -> tuple[frozenset[str], frozenset[str]]:
    """(entries the page loads as style sheets, entries it names otherwise), as package paths.

    Read on the page as it was received, with the attribute names compared in lower case: it
    must see at least what a browser would load.
    """
    root = _parse(data)
    base = posixpath.dirname(path)
    sheets: set[str] = set()
    others: set[str] = set()
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = _split(el.tag)[1].lower()
        rel = el.attrib.get("rel", "").lower().split()
        for key, value in el.attrib.items():
            if _split(key)[1].lower() not in ("href", "src") or not value:
                continue
            target = _resolve(base, value)
            if target:
                (sheets if local == "link" and "stylesheet" in rel else others).add(target)
    return frozenset(sheets), frozenset(others)
