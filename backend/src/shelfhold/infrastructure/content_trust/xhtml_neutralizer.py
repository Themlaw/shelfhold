"""xhtml-neutralizer@v1: the one producer of the derivative of an `untrusted` EPUB (EF-BIB-051).

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
  unwrapped (its text stays) and what is dangerous is dropped with its whole subtree. Element and
  attribute names are compared in lower case too, since the reader's HTML parser does.
* URLs are checked on a squashed copy (no control, space or invisible character, lower case, `\\`
  read as `/`), the way a browser's parser would, whatever entities wrote them. Only relative
  references pass, plus `http`, `https` and `mailto` on `a href` (a link is a navigation, not a
  resource) and small raster `data:image` URLs on images. Anything with another scheme, a leading
  `//` or `/` is dropped.
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
import re
import unicodedata
from html.parser import HTMLParser
from typing import Final, Literal

import nh3

from shelfhold.infrastructure.content_trust.cbz_entries import UNTRUSTED_ENTRY_CODE
from shelfhold.infrastructure.xml import Element, UnsafeXmlError, XmlLimits, safe_parse_xml

Kind = Literal["xhtml", "svg", "css"]

NEUTRALIZER_VERSION: Final = 1
DERIVED_BY: Final = f"xhtml-neutralizer@v{NEUTRALIZER_VERSION}"

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
_LINK_SCHEMES: Final = frozenset({"http", "https", "mailto"})
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


def _is_fragment(value: str) -> bool:
    return _squash(value).startswith("#")


def _link_ok(value: str) -> bool:
    s = _squash(value)
    if not s or s.startswith("//"):
        return False
    found = _SCHEME.match(s)
    if found:
        return s[: found.end() - 1] in _LINK_SCHEMES
    return not s.startswith("/")


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


def _css_split(text: str) -> tuple[str, str] | None:
    """(text without comments, the same with every string emptied), or None if it is not closed."""
    out: list[str] = []
    bare: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                return None
            out.append(" ")
            bare.append(" ")
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


def _css_unsafe(chunk: str) -> bool:
    """True if a rule, or a declaration list, must not survive."""
    if any(ch in "<&" for ch in chunk) or "]]>" in chunk:
        return True
    if any(unicodedata.category(ch) == "Cc" and ch not in "\t\n\r\f" for ch in chunk):
        return True
    split = _css_split(chunk)
    if split is None:
        return True
    text, bare = split
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
    """The style sheet without the rules that fetch, import or run something."""
    return "".join(c for c in _css_chunks(text) if not _css_unsafe(c) or not c.strip())


def _clean_style_attr(value: str) -> str | None:
    return None if _css_unsafe(value) else value


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


class _Writer:
    """Walks the parsed tree and writes the allowlisted subset as a new document."""

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
                return _link_ok(value)
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
        parts = [_esc_text(el.text or "")]
        for child in el:
            parts.append(self.element(child, context))
            parts.append(_esc_text(child.tail or ""))
        return "".join(parts)

    def element(self, el: Element, context: str) -> str:
        if not isinstance(el.tag, str):
            return ""
        ns, local = _split(el.tag)
        low = local.lower()
        if low in _DANGEROUS:
            return ""
        if ns == SVG_NS or (ns is None and context == "xhtml" and local == "svg"):
            context = "svg"
        elif ns not in (XHTML_NS, None):
            return self.children(el, context)
        if context == "svg":
            return self._svg(el, local)
        if local not in _XHTML_TAGS:
            return self.children(el, context)
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

    def _svg(self, el: Element, local: str) -> str:
        if local not in _SVG_TAGS:
            return self.children(el, "svg")
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
        return self._start("link", attrs) + "/>"

    def _meta(self, el: Element) -> str:
        if any(_split(k)[1].lower() == "http-equiv" for k in el.attrib):
            return ""
        attrs = self._attributes(el, "meta", "xhtml")
        names = {k for k, _ in attrs}
        if not (names == {"charset"} or names == {"name", "content"}):
            return ""
        return self._start("meta", attrs) + "/>"


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


def _parse(data: bytes) -> Element:
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
                parts.append(writer.element(child, "xhtml"))
    return "".join(parts)


def _xhtml(data: bytes) -> bytes:
    root = _parse(data)
    ns, local = _split(root.tag)
    if local.lower() != "html" or ns not in (XHTML_NS, None):
        raise NeutralizationError("root")
    writer = _Writer()
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


def _svg_document(data: bytes) -> bytes:
    root = _parse(data)
    ns, local = _split(root.tag)
    if local != "svg" or ns not in (SVG_NS, None):
        raise NeutralizationError("root")
    written = _Writer().element(root, "svg")
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
    """The neutralized form of one XHTML page, SVG image or style sheet (`xhtml-neutralizer@v1`).

    Pure and deterministic. Raises `NeutralizationError` for a document it refuses to touch
    (unsafe XML, wrong root, a disagreement with nh3): the EPUB it belongs to is then not exported.
    """
    if kind == "xhtml":
        return _xhtml(data)
    if kind == "svg":
        return _svg_document(data)
    if kind == "css":
        return _css(data)
    raise ValueError(f"unknown kind {kind!r}")
