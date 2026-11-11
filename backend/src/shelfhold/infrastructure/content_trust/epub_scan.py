"""Reading an `untrusted` EPUB and writing its neutralized derivative (EF-BIB-051 (2), (7)).

The archive is opened only by `safe_open_archive` (traversal, absolute and duplicate names, links,
bombs, entry caps) and every XML document it holds is read by `safe_parse_xml`. The EPUB is then
copied entry by entry, in the same order, to the stream the caller owns. **Every entry must fall
in exactly one category, and an entry that falls in none refuses the EPUB** (`unexpected_entry`);
nothing is recognised by looking for markup in bytes, which any padding defeats.

1. `mimetype`, `META-INF/*` and the OPF are copied byte for byte and never rewritten (this module
   cannot even reach the OPF writer, contract `opf-sole-writer`). `container.xml` and every OPF
   are first **read in full**: a processing instruction other than the XML declaration, an
   `href`, `src`, `refines` or `xml:base` that is not relative (tested on the raw value, then on
   the decoded one), a reference that leaves the package root, or any XHTML or SVG element refuses
   the EPUB.
2. XHTML, SVG and style sheets are always neutralized, whether the manifest media type or the
   extension says what they are. If both say something and disagree, the EPUB is refused.
3. NCX and XML (declared as XML, or named `.xml` or `.ncx`) are read in full: an element of the
   XHTML or SVG namespace, or any processing instruction, refuses the EPUB; otherwise they are
   copied.
4. Images (png, jpeg, gif, webp) and fonts (ttf, otf, woff, woff2) are copied if and only if their
   first bytes are the magic bytes of their extension. No markup is searched for in a font: an
   OpenType-SVG font is accepted, since a reader does not run it as a page.
5. Anything else (`.xsl`, `.js`, `.dat`, `.svgz`, audio, an entry with no known extension) is
   refused.

Paths are resolved against the OPF; a `href` that leaves the package root is refused.

There is no parameter that says how far the source is trusted: this function is only called for an
`untrusted` EPUB, and a watermark found by a heuristic never changes what it does.

`derived_hash` is the hash of the derivative **without its OPF**, in the framing of
`content_hash@1` (name and bytes of every entry, in order) under the name `content-hash@2`, the
EPUB flavour (the OPF is rewritten by the export, so it is not part of the payload). The same
function gives the hash of the original, so "the neutralizer changed nothing" is a comparison.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final
from urllib.parse import unquote

from shelfhold.infrastructure.archives import (
    DEFAULT_LIMITS,
    ArchiveLimits,
    ArchiveWriter,
    SafeArchive,
    UnsafeArchiveError,
    safe_open_archive,
)
from shelfhold.infrastructure.content_trust.xhtml_neutralizer import (
    DERIVED_BY,
    XML_LIMITS_INSPECT,
    NeutralizationError,
    is_relative_reference,
    neutralize,
    parse_xml_document,
)
from shelfhold.infrastructure.xml import Element, XmlLimits

DERIVED_HASH_VERSION: Final = 2
DERIVED_HASH_BY: Final = f"content-hash@{DERIVED_HASH_VERSION}"

_MIMETYPE: Final = b"application/epub+zip"
_XML_LIMITS: Final = XmlLimits(max_bytes=4 * 1024 * 1024)
_PAGE_BYTES: Final = 32 * 1024 * 1024
_CHUNK: Final = 1 << 20
_PAGE_FAMILIES: Final = frozenset({"xhtml", "svg", "css"})
_EXT_FAMILY: Final = {
    ".xhtml": "xhtml", ".xht": "xhtml", ".html": "xhtml", ".htm": "xhtml", ".svg": "svg",
    ".css": "css", ".xml": "xml", ".ncx": "xml", ".opf": "opf",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".ttf": "font", ".otf": "font", ".woff": "font", ".woff2": "font",
}  # fmt: skip
_MEDIA_FAMILY: Final = {
    "application/xhtml+xml": "xhtml", "text/html": "xhtml", "image/svg+xml": "svg",
    "text/css": "css", "text/xml": "xml", "application/xml": "xml",
    "application/x-dtbncx+xml": "xml", "image/png": "image", "image/jpeg": "image",
    "image/gif": "image", "image/webp": "image", "application/oebps-package+xml": "opf",
}  # fmt: skip
_META_INF_XML: Final = frozenset(
    f"META-INF/{n}" for n in ("manifest.xml", "metadata.xml", "rights.xml", "signatures.xml")
)
_XML_EXTS: Final = frozenset({"", ".xml", ".ncx"})
_STORED_EXT: Final = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2"})
_DECODED_REMOTE: Final = re.compile(r"[a-z][a-z0-9+.\-]*://|//")
_PI: Final = re.compile(rb"<\?(?!xml[\s?])", re.I)
_BAD_ELEMENTS: Final = frozenset({"html", "svg", "script", "body", "iframe", "object", "embed"})
_PAGE_NAMESPACES: Final = ("http://www.w3.org/1999/xhtml", "http://www.w3.org/2000/svg")
_URL_ATTRS: Final = frozenset({"href", "src", "refines", "base"})
_MAGIC: Final = {
    ".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"), ".woff": (b"wOFF",), ".woff2": (b"wOF2",),
    ".ttf": (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO"),
    ".otf": (b"OTTO", b"\x00\x01\x00\x00", b"ttcf"),
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Derivative:
    """What the pipeline records: `derived_by`, `derived_hash` and `derived_hash_by`."""

    derived_by: str
    derived_hash: str
    derived_hash_by: str
    original_hash: (
        str  # the same hash over the original: equal to `derived_hash` if nothing changed
    )
    neutralized: tuple[str, ...]  # entries whose bytes were rewritten

    @property
    def changed(self) -> bool:
        return self.derived_hash != self.original_hash


class _PayloadHash:
    """`content-hash@2`: entries in order, name and bytes, the OPF left out."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256(f"{DERIVED_HASH_BY}\n".encode())

    def add(self, name: str, page_digest: bytes) -> None:
        if name.lower().endswith(".opf"):
            return
        raw = name.encode("utf-8", "surrogatepass")
        self._digest.update(len(raw).to_bytes(4, "big") + raw + page_digest)

    def hexdigest(self) -> str:
        return "sha256:" + self._digest.hexdigest()


def epub_payload_hash(archive: SafeArchive) -> str:
    """`content-hash@2` of an EPUB (every entry but the OPF)."""
    payload = _PayloadHash()
    for entry in archive.entries:
        if entry.is_dir:
            continue
        page = hashlib.sha256()
        with archive.open(entry.name) as stream:
            while chunk := stream.read(_CHUNK):
                page.update(chunk)
        payload.add(entry.name, page.digest())
    return payload.hexdigest()


def _ext(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _read_xml(archive: SafeArchive, name: str, reason: str) -> Element:
    """Read an XML entry in full: size and shape by `safe_parse_xml`, no PI, no page element."""
    try:
        data = bytes(archive.read(name, max_bytes=_XML_LIMITS.max_bytes))
    except KeyError:
        raise NeutralizationError(reason, name) from None
    if _PI.search(data.replace(b"\x00", b"")):  # UTF-16 and UTF-32 too
        raise NeutralizationError("processing_instruction", name)
    try:
        root = parse_xml_document(data, XML_LIMITS_INSPECT)
    except NeutralizationError as error:
        raise NeutralizationError(error.reason, name) from None
    for element in root.iter():
        tag = str(element.tag)
        if tag.startswith("{") and tag[1:].partition("}")[0] in _PAGE_NAMESPACES:
            raise NeutralizationError("page_in_xml", name)
        if _local(tag).lower() in _BAD_ELEMENTS:
            raise NeutralizationError("page_in_xml", name)
    return root


def _checked_reference(raw: str, base: str, owner: str) -> str:
    """The package path a reference names, or a refusal if it is not inside the package.

    The scheme is read on the raw value (a `%3A` is not a colon to a URL parser, so a chapter
    called `Part 1: Intro` is fine), the root and the parent directories on the decoded one.
    """
    decoded = unquote(raw).replace("\\", "/")
    if (
        not raw
        or not is_relative_reference(raw)
        or decoded.startswith("/")
        or _DECODED_REMOTE.match(decoded.strip().lower())
    ):
        raise NeutralizationError("remote_reference", owner)
    path = posixpath.normpath(posixpath.join(base, decoded.split("#", 1)[0] or "."))
    if path == ".." or path.startswith("../") or path.startswith("/"):
        raise NeutralizationError("remote_reference", owner)
    return path


def _media_type(value: str | None) -> str | None:
    return value.split(";", 1)[0].strip().lower() if value else None


def _read_package(archive: SafeArchive, opf: str, declared: dict[str, str | None]) -> None:
    base = posixpath.dirname(opf)
    root = _read_xml(archive, opf, "opf")
    for element in root.iter():
        for key, value in element.attrib.items():
            if _local(key) not in _URL_ATTRS:
                continue
            # `refines` is a fragment (`#id`); `xml:base` and every href or src is a path
            path = _checked_reference(value, base, opf)
            if _local(element.tag) == "item" and _local(key) == "href":
                if path.startswith("META-INF/"):  # the container's folder is not a place for pages
                    raise NeutralizationError("meta_inf_item", opf)
                media = _media_type(element.attrib.get("media-type"))
                family = _MEDIA_FAMILY.get(media or "")
                if path in declared and declared[path] != family:
                    raise NeutralizationError("type_mismatch", opf)
                declared[path] = family


def _manifest(archive: SafeArchive) -> tuple[set[str], dict[str, str | None]]:
    """The OPF paths and the family each manifest item declares (everything is read first)."""
    names = {e.name for e in archive.entries}
    container = _read_xml(archive, "META-INF/container.xml", "container")
    opfs: list[str] = []
    for element in container.iter():
        if _local(element.tag) == "rootfile" and element.attrib.get("full-path"):
            opfs.append(
                _checked_reference(element.attrib["full-path"], "", "META-INF/container.xml")
            )
    if not opfs:
        raise NeutralizationError("container")
    declared: dict[str, str | None] = {}
    for opf in opfs:
        if opf not in names:
            raise NeutralizationError("container", opf)
        _read_package(archive, opf, declared)
    for name in names:  # an OPF the container does not name is read like the others
        if _ext(name) == ".opf" and name not in opfs:
            _read_package(archive, name, declared)
            opfs.append(name)
    return set(opfs), declared


def _classify(
    archive: SafeArchive, name: str, opfs: set[str], declared: dict[str, str | None]
) -> str:
    """`copy`, or the page kind to neutralize; a refusal if the entry is in no category."""
    if name == "mimetype" or name in opfs or name == "META-INF/container.xml":
        return "copy"
    if name == "META-INF/encryption.xml":  # an encrypted package cannot be read, so not checked
        raise NeutralizationError("encrypted", name)
    if name in _META_INF_XML:  # known container files: read in full, copied as they are
        _read_xml(archive, name, "xml")
        return "copy"
    # any other entry under META-INF/ is classified like any entry: a page is neutralized, what
    # is in no category refuses the EPUB (it used to be copied whatever it held)
    ext = _ext(name)
    by_ext = _EXT_FAMILY.get(ext)
    by_type = declared.get(name)
    if by_ext and by_type and by_ext != by_type:
        raise NeutralizationError("type_mismatch", name)
    family = by_ext or by_type
    if family in _PAGE_FAMILIES:
        return family
    if family == "xml" and ext in _XML_EXTS:
        _read_xml(archive, name, "xml")
        return "copy"
    if family in ("image", "font") and by_ext:
        head = archive.read_head(name, 8)
        if not head.startswith(_MAGIC[ext]):
            raise NeutralizationError("magic_mismatch", name)
        return "copy"
    raise NeutralizationError("unexpected_entry", name)


def _check_mimetype(archive: SafeArchive) -> None:
    first = archive.entries[0] if archive.entries else None
    if first is None or first.name != "mimetype":
        raise NeutralizationError("mimetype")
    if first.compressed_size != first.size or bytes(archive.read("mimetype", 64)) != _MIMETYPE:
        raise NeutralizationError("mimetype")


class _Counting:
    """A read-only view of an entry that hashes what passes through it."""

    def __init__(self, stream: object, digest: hashlib._Hash) -> None:
        self._stream = stream
        self._digest = digest

    def read(self, size: int = -1) -> bytes:
        chunk: bytes = self._stream.read(size)  # type: ignore[attr-defined]
        self._digest.update(chunk)
        return chunk


def neutralize_epub(
    source: Path | BinaryIO,
    out: BinaryIO,
    *,
    name: str = "book.epub",
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> Derivative:
    """Write the neutralized derivative of the EPUB `source` to `out` and describe it.

    Raises `NeutralizationError` (stable code `content.untrusted_entry`) if the archive or one of
    its XML documents is unsafe; `out` then holds nothing worth keeping.
    """
    try:
        with safe_open_archive(source, name=name, limits=limits) as archive:
            return _derive(archive, out)
    except UnsafeArchiveError as error:
        raise NeutralizationError(error.reason, error.entry) from None


def _derive(archive: SafeArchive, out: BinaryIO) -> Derivative:
    _check_mimetype(archive)
    opfs, declared = _manifest(archive)
    original, derived = _PayloadHash(), _PayloadHash()
    changed: list[str] = []
    with ArchiveWriter(out) as writer:
        for entry in archive.entries:
            entry_name = entry.name
            if entry.is_dir:
                writer.add(entry_name, b"")
                continue
            kind = _classify(archive, entry_name, opfs, declared)
            if kind == "copy":
                before = hashlib.sha256()
                stored = _ext(entry_name) in _STORED_EXT or entry_name == "mimetype"
                with archive.open(entry_name) as stream:
                    writer.add(entry_name, _Counting(stream, before), compress=not stored)  # type: ignore[arg-type]
                original.add(entry_name, before.digest())
                derived.add(entry_name, before.digest())
                continue
            data = bytes(archive.read(entry_name, _PAGE_BYTES))
            try:
                result = neutralize(data, kind)  # type: ignore[arg-type]
            except NeutralizationError as error:
                raise NeutralizationError(error.reason, entry_name) from None
            writer.add(entry_name, result, compress=True)
            original.add(entry_name, hashlib.sha256(data).digest())
            derived.add(entry_name, hashlib.sha256(result).digest())
            if result != data:
                changed.append(entry_name)
    return Derivative(
        derived_by=DERIVED_BY,
        derived_hash=derived.hexdigest(),
        derived_hash_by=DERIVED_HASH_BY,
        original_hash=original.hexdigest(),
        neutralized=tuple(changed),
    )
