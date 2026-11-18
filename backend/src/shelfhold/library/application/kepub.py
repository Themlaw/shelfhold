"""kepubify: the pure-Python KEPUB core (EF-BIB-015, M5 L20b).

One function, `kepubify(data)`: the bytes of an EPUB (already neutralized, but treated as
untrusted all the same) become the bytes of a KEPUB for a Kobo reader, in Python only: no
external binary is run and nothing is written to the file system.

The reading order is walked from the OPF. Each XHTML document of the spine — the navigation
document excepted — receives the Kobo spans around the sentences of its paragraphs:
`<span class="koboSpan" id="kobo.N.M">`, N the paragraph (1-based, document order) and M the
segment (1-based, reset at each paragraph), the numbering kepubify uses. A paragraph is a
block text container (`p`, `h1`-`h6`, `li`, `dd`, `dt`, `td`, `th`, `caption`, `figcaption`,
`blockquote`); a segment is a sentence, cut after `.`, `!`, `?` and their CJK counterparts
followed by whitespace; a text node with no sentence boundary is one segment, and whitespace-only
text is left as it is. The body content is then wrapped in the `book-inner` / `book-columns`
divs. Every other entry — the OPF, the spine, nav.xhtml, toc.ncx, the metadata, the images, the
style sheets — is copied byte for byte, in its original order; `mimetype` stays first and stored.

The archive is read through `safe_open_archive` (the single archive entry point: the existing
bounds of the repository — entry count, sizes, compression ratio, overlap) and written by
`ArchiveWriter` (fixed timestamps: the same bytes always give the same bytes). XML is read only
by `safe_parse_xml` (no DTD, no external or parameter entity, size, depth and element caps): a
document that declares an entity is refused before any expansion, never resolved. Every refusal
is this module's own `KepubError`, a `ValueError` with a stable `reason` word — never a 500.

This module is the core only: no OPDS feed, no `/lite` route, no cache, no wiring of any kind
(those depend on lots L20a and L17a, not delivered here; see docs/decisions/M5.md).
"""

from __future__ import annotations

import io
import posixpath
import re
import urllib.parse
from typing import BinaryIO, Final

from shelfhold.infrastructure.archives import (
    DEFAULT_LIMITS,
    ArchiveLimits,
    ArchiveWriter,
    SafeArchive,
    UnsafeArchiveError,
    safe_open_archive,
)
from shelfhold.infrastructure.xml import Element, UnsafeXmlError, XmlLimits, safe_parse_xml

__all__ = ["KepubError", "kepubify"]

XHTML_NS: Final = "http://www.w3.org/1999/xhtml"
SVG_NS: Final = "http://www.w3.org/2000/svg"
XLINK_NS: Final = "http://www.w3.org/1999/xlink"
EPUB_NS: Final = "http://www.idpf.org/2007/ops"
XML_NS: Final = "http://www.w3.org/XML/1998/namespace"
OPF_MEDIA_TYPE: Final = "application/oebps-package+xml"
XHTML_TYPES: Final = frozenset({"application/xhtml+xml", "text/html"})
MIMETYPE: Final = "mimetype"
CONTAINER: Final = "META-INF/container.xml"

XML_LIMITS: Final = XmlLimits()  # 8 MiB per document, on top of the archive bounds
_PROLOG: Final = '<?xml version="1.0" encoding="utf-8"?>\n'
_PARAGRAPHS: Final = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dd", "dt", "td", "th", "caption",
     "figcaption", "blockquote"}
)  # fmt: skip
_SENTENCE: Final = re.compile(r"[.!?\u3002\uFF01\uFF1F]+[\"'\u2019\u201D\u00BB)\]]*\s+")
_KNOWN_PREFIXES: Final = {XHTML_NS: "", SVG_NS: "svg", XLINK_NS: "xlink", EPUB_NS: "epub"}


class KepubError(ValueError):
    """The EPUB is refused. `reason` is a stable machine-readable word."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


def kepubify(data: bytes, *, limits: ArchiveLimits = DEFAULT_LIMITS) -> bytes:
    """The KEPUB form of an EPUB, or `KepubError` (never the error of a layer below)."""
    if not isinstance(data, bytes | bytearray):
        raise TypeError("kepubify expects bytes")
    try:
        with safe_open_archive(io.BytesIO(bytes(data)), name="book.epub", limits=limits) as archive:
            changed = _spine_documents(archive)
            out = io.BytesIO()
            _write(archive, changed, out)
            return out.getvalue()
    except UnsafeArchiveError as error:
        raise KepubError(error.reason, str(error)) from None
    except UnsafeXmlError as error:
        raise KepubError(f"xml_{error.reason}", str(error)) from None


# --- the spine -----------------------------------------------------------------------------------


def _read_xml(archive: SafeArchive, name: str) -> Element:
    return safe_parse_xml(archive.read(name, max_bytes=XML_LIMITS.max_bytes), limits=XML_LIMITS)


def _opf_path(archive: SafeArchive, names: set[str]) -> str:
    if CONTAINER not in names:
        raise KepubError("no_container", f"{CONTAINER} is missing")
    root = _read_xml(archive, CONTAINER)
    roots = [r for r in root.iter() if _local(r.tag) == "rootfile" and r.get("full-path")]
    preferred = [r for r in roots if r.get("media-type") == OPF_MEDIA_TYPE] or roots
    if not preferred:
        raise KepubError("no_rootfile", "the container names no package document")
    path = str(preferred[0].get("full-path"))
    if path not in names:
        raise KepubError("opf_missing", path)
    return path


def _resolve(base: str, href: str) -> tuple[str, str] | None:
    """(archive path, fragment) of `href` read from the document at `base`, or None when it
    leaves the archive or is not a plain relative reference."""
    parsed = urllib.parse.urlsplit(href.strip())
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    path = urllib.parse.unquote(parsed.path)
    if path.startswith("/") or "\x00" in path or "\\" in path:
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
    if joined.startswith("../") or joined == "..":
        return None
    return joined, urllib.parse.unquote(parsed.fragment)


def _reading_order(package: Element, opf_path: str, names: set[str]) -> list[tuple[str, str, str]]:
    """(path, media type, properties) of each spine item, in order; a spine item that points at
    no item of the archive makes the EPUB invalid: it is refused, not skipped."""
    manifest: dict[str, tuple[str, str, str]] = {}
    for item in package.iter():
        if _local(item.tag) != "item":
            continue
        item_id, href = item.get("id"), item.get("href")
        if not item_id or not href:
            continue
        resolved = _resolve(opf_path, href)
        if resolved is None or resolved[0] not in names:
            continue
        manifest[str(item_id)] = (
            resolved[0], item.get("media-type", ""), item.get("properties", ""),
        )  # fmt: skip
    order: list[tuple[str, str, str]] = []
    for ref in package.iter():
        if _local(ref.tag) != "itemref" or not ref.get("idref"):
            continue
        entry = manifest.get(str(ref.get("idref")))
        if entry is None:
            raise KepubError(
                "spine", f"itemref {ref.get('idref')} points at no item of the archive"
            )
        order.append(entry)
    return order


def _spine_documents(archive: SafeArchive) -> dict[str, bytes]:
    """The transformed bytes of each XHTML document of the spine, by archive path."""
    names = {entry.name for entry in archive.entries}
    if MIMETYPE not in names:
        raise KepubError("mimetype", "no mimetype entry")
    opf_path = _opf_path(archive, names)
    changed: dict[str, bytes] = {}
    for path, media_type, properties in _reading_order(
        _read_xml(archive, opf_path), opf_path, names
    ):
        if path in changed or media_type not in XHTML_TYPES:
            continue
        if "nav" in properties.split():
            continue  # the navigation document keeps its bytes, spine or not
        changed[path] = _kepub_page(_read_xml(archive, path))
    return changed


# --- one page ------------------------------------------------------------------------------------


def _kepub_page(root: Element) -> bytes:
    ns, local = _split(root.tag)
    if ns not in (XHTML_NS, None) or local.lower() != "html":
        raise KepubError("page", "the spine document is not XHTML")
    bodies = [b for b in root if _split(b.tag)[1].lower() == "body"]
    if len(bodies) != 1:
        raise KepubError("page", "a spine document needs exactly one body")
    _Spanner().run(bodies[0])
    _wrap_body(bodies[0])
    return (_Serializer().document(root)).encode()


def _split(name: str) -> tuple[str | None, str]:
    if name.startswith("{"):
        namespace, _, local = name[1:].partition("}")
        return namespace, local
    return None, name


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _segments(text: str) -> list[str]:
    """The sentences of one text node: cut after a sentence ender and the whitespace that
    follows it; the pieces joined back are exactly the original text."""
    parts: list[str] = []
    start = 0
    for boundary in _SENTENCE.finditer(text):
        parts.append(text[start : boundary.end()])
        start = boundary.end()
    parts.append(text[start:])
    return parts


class _Spanner:
    """Adds the Kobo spans to the paragraphs of one page, in document order."""

    def __init__(self) -> None:
        self._paragraphs = 0

    def run(self, body: Element) -> None:
        self._walk(body)

    def _walk(self, element: Element) -> None:
        for child in list(element):
            if self._is_paragraph(child):
                self._paragraph(child)
            elif not self._is_svg(child):
                self._walk(child)

    def _paragraph(self, element: Element) -> None:
        self._paragraphs += 1
        n = self._paragraphs
        children = list(element)  # before the spans of the own text shift the positions
        m = self._slot(element, None, n, 0, 0)
        at = m  # the spans of the own text pushed the first child to this live index
        for child in children:
            if self._is_paragraph(child):
                self._paragraph(child)  # a nested paragraph is numbered on its own
            elif not self._is_svg(child):
                m = self._inline(child, n, m)
            before = m
            m = self._slot(element, child, n, m, at + 1)
            at += 1 + m - before

    def _inline(self, element: Element, n: int, m: int) -> int:
        children = list(element)  # before the spans of the own text shift the positions
        before = m
        m = self._slot(element, None, n, m, 0)
        at = m - before
        for child in children:
            if self._is_paragraph(child):
                self._paragraph(child)
            elif not self._is_svg(child):
                m = self._inline(child, n, m)
            before = m
            m = self._slot(element, child, n, m, at + 1)
            at += 1 + m - before
        return m

    def _slot(self, holder: Element, child: Element | None, n: int, m: int, at: int) -> int:
        """Wrap the text at one slot (the holder's own text, or the tail of `child`) in one
        span per segment, inserted at live index `at`. Whitespace-only text is left as it is,
        unwrapped and unnumbered."""
        text = holder.text if child is None else child.tail
        if not text or not text.strip():
            return m
        spans: list[Element] = []
        for segment in _segments(text):
            m += 1
            span = Element(f"{{{XHTML_NS}}}span", {"class": "koboSpan", "id": f"kobo.{n}.{m}"})
            span.text = segment
            spans.append(span)
        if child is None:
            holder.text = ""
        else:
            child.tail = ""
        for offset, span in enumerate(spans):
            holder.insert(at + offset, span)
        return m

    @staticmethod
    def _is_paragraph(element: Element) -> bool:
        namespace, local = _split(element.tag)
        return namespace in (XHTML_NS, None) and local.lower() in _PARAGRAPHS

    @staticmethod
    def _is_svg(element: Element) -> bool:
        return _split(element.tag)[0] == SVG_NS  # its text is not the paragraph's to wrap


def _wrap_body(body: Element) -> None:
    """The body content sits inside the two Kobo divs, in this order."""
    inner = Element(f"{{{XHTML_NS}}}div", {"class": "book-inner"})
    columns = Element(f"{{{XHTML_NS}}}div", {"class": "book-columns"})
    columns.text = body.text
    for child in list(body):
        columns.append(child)
    del body[:]
    body.text = None
    inner.append(columns)
    body.append(inner)


# --- the writer ----------------------------------------------------------------------------------


_FORBIDDEN: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")


def _esc_text(text: str) -> str:
    if _FORBIDDEN.search(text):
        raise KepubError("character", "a character XML 1.0 cannot write")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(text: str) -> str:
    return (
        _esc_text(text).replace('"', "&quot;").replace("\t", "&#9;").replace("\n", "&#10;")
        .replace("\r", "&#13;")
    )  # fmt: skip


class _Serializer:
    """Writes the parsed tree back as one deterministic XML document.

    The prefixes come from a fixed table (svg, xlink, epub); anything else gets `ns0`, `ns1`, …
    in order of first appearance. The XHTML namespace is the default one, declared on the root.
    The default namespace in effect is tracked element by element: an element with no namespace
    under a non-empty default one is written with `xmlns=""`, or it would change space on the
    way back. The parser can never hand over a character XML 1.0 forbids; the writer refuses one
    anyway (`KepubError`), rather than emit a document no reader accepts.
    """

    def __init__(self) -> None:
        self._default: str | None = None  # the default namespace in effect
        self._prefixes: dict[str, str] = {}  # namespace -> prefix

    def document(self, root: Element) -> str:
        return _PROLOG + self._element(root)

    def _element(self, el: Element) -> str:
        saved_default = self._default
        saved_prefixes = dict(self._prefixes)
        decls: list[str] = []
        qname, decl = self._name(el.tag)
        if decl:
            decls.append(decl)
        attrs: list[str] = []
        for key, value in el.attrib.items():
            name, decl = self._attribute_name(key)
            if decl:
                decls.append(decl)
            attrs.append(f' {name}="{_esc_attr(value)}"')
        start = f"<{qname}{''.join(attrs)}{''.join(f' {d}' for d in decls)}"
        inner = _esc_text(el.text or "")
        for child in el:
            inner += self._element(child)
            inner += _esc_text(child.tail or "")
        self._default = saved_default
        self._prefixes = saved_prefixes
        return f"{start}/>" if not inner else f"{start}>{inner}</{qname}>"

    def _name(self, tag: str) -> tuple[str, str]:
        namespace, local = _split(tag)
        if namespace is None:
            if self._default:  # a bare element would otherwise ride the default namespace
                self._default = None
                return local, 'xmlns=""'
            return local, ""
        if namespace == self._default:
            return local, ""
        prefix = self._prefixes.get(namespace)
        if prefix is not None:
            return f"{prefix}:{local}", ""
        if _KNOWN_PREFIXES.get(namespace) == "":  # XHTML rides the default namespace
            self._default = namespace
            return local, f'xmlns="{_esc_attr(namespace)}"'
        prefix = _KNOWN_PREFIXES.get(namespace, f"ns{len(self._prefixes)}")
        self._prefixes[namespace] = prefix
        return f"{prefix}:{local}", f'xmlns:{prefix}="{_esc_attr(namespace)}"'

    def _attribute_name(self, key: str) -> tuple[str, str]:
        namespace, local = _split(key)
        if namespace is None:
            return local, ""  # an unprefixed attribute is in no namespace, whatever the default
        if namespace == XML_NS:
            return f"xml:{local}", ""  # predefined, never declared
        prefix = self._prefixes.get(namespace)
        if prefix is None:
            prefix = _KNOWN_PREFIXES.get(namespace) or f"ns{len(self._prefixes)}"
            self._prefixes[namespace] = prefix
            return f"{prefix}:{local}", f'xmlns:{prefix}="{_esc_attr(namespace)}"'
        return f"{prefix}:{local}", ""


def _write(archive: SafeArchive, changed: dict[str, bytes], out: BinaryIO) -> None:
    """`mimetype` first and stored, then every entry in its original order; an unchanged entry
    keeps the compression of the source, a transformed one is deflated."""
    with ArchiveWriter(out) as writer:
        writer.add(MIMETYPE, bytes(archive.read(MIMETYPE)), compress=False)
        for entry in archive.entries:
            if entry.name == MIMETYPE:
                continue
            if entry.is_dir:
                writer.add(entry.name, b"")
            elif entry.name in changed:
                writer.add(entry.name, changed[entry.name], compress=True)
            else:
                packed = entry.compressed_size < entry.size
                with archive.open(entry.name) as data:
                    writer.add(entry.name, data, compress=packed)
