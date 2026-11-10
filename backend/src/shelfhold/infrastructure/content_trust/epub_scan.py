"""Reading an `untrusted` EPUB and writing its neutralized derivative (EF-BIB-051 (2), (7)).

The archive is opened only by `safe_open_archive` (traversal, absolute and duplicate names, links,
bombs, entry caps) and every XML document it holds is read by `safe_parse_xml`. The EPUB is then
copied entry by entry, in the same order, to the stream the caller owns:

* `mimetype` (first, stored, `application/epub+zip`), the OPF (any `.opf`) and `META-INF/` are
  copied byte for byte and never rewritten: this module cannot even reach the OPF writer
  (contract `opf-sole-writer`);
* a XHTML page, a SVG image or a style sheet goes through `neutralize` (the manifest media type or
  the extension decides; a `.xml` or `.ncx` that is really a page is a page);
* every other entry is copied byte for byte. An image, font or media entry whose first bytes are a
  page or an SVG is a document in disguise and refuses the EPUB.

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
    NeutralizationError,
    is_relative_reference,
    neutralize,
)
from shelfhold.infrastructure.xml import UnsafeXmlError, XmlLimits, safe_parse_xml

DERIVED_HASH_VERSION: Final = 2
DERIVED_HASH_BY: Final = f"content-hash@{DERIVED_HASH_VERSION}"

_MIMETYPE: Final = b"application/epub+zip"
_XML_LIMITS: Final = XmlLimits(max_bytes=4 * 1024 * 1024)
_PAGE_BYTES: Final = 32 * 1024 * 1024
_SNIFF: Final = 8192
_CHUNK: Final = 1 << 20
_XHTML_EXT: Final = frozenset({".xhtml", ".xht", ".html", ".htm"})
_MEDIA_KIND: Final = {
    "application/xhtml+xml": "xhtml", "text/html": "xhtml", "image/svg+xml": "svg",
    "text/css": "css", "text/xml": "sniff", "application/xml": "sniff",
}  # fmt: skip
_EXT_KIND: Final = {".xhtml": "xhtml", ".xht": "xhtml", ".html": "xhtml", ".htm": "xhtml",
                    ".svg": "svg", ".css": "css"}  # fmt: skip
_XML_LIKE_EXT: Final = frozenset({".xml", ".ncx"})
_BINARY_EXT: Final = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".jxl", ".bmp", ".ico", ".tif", ".tiff",
     ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".m4a", ".m4v", ".ogg", ".oga",
     ".ogv", ".wav", ".webm", ".opus", ".aac", ".jp2", ".heic"}
)  # fmt: skip
_STORED_EXT: Final = _BINARY_EXT | {".zip", ".gz", ".epub", ".pdf"}
_PAGE_MARK: Final = re.compile(rb"<\s*(?:svg|html|script)\b|<!doctype\s+html", re.I)
_SVG_MARK: Final = re.compile(rb"<\s*svg\b", re.I)


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


def _xml(archive: SafeArchive, name: str, reason: str) -> object:
    try:
        return safe_parse_xml(
            archive.read(name, max_bytes=_XML_LIMITS.max_bytes), limits=_XML_LIMITS
        )
    except UnsafeXmlError as error:
        raise NeutralizationError(f"{reason}_{error.reason}", name) from None
    except KeyError:
        raise NeutralizationError(reason, name) from None


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _manifest_kinds(archive: SafeArchive) -> tuple[set[str], dict[str, str]]:
    """The OPF paths named by container.xml and the kind of every manifest item (read only)."""
    names = {e.name for e in archive.entries}
    container = _xml(archive, "META-INF/container.xml", "container")
    opfs = [
        e.attrib["full-path"]
        for e in container.iter()  # type: ignore[attr-defined]
        if _local(e.tag) == "rootfile" and e.attrib.get("full-path")
    ]
    if not opfs:
        raise NeutralizationError("container")
    kinds: dict[str, str] = {}
    for opf in opfs:
        if opf not in names:
            raise NeutralizationError("container", opf)
        base = posixpath.dirname(opf)
        package = _xml(archive, opf, "opf")
        for item in package.iter():  # type: ignore[attr-defined]
            if _local(item.tag) not in ("item", "reference"):
                continue
            href = unquote(item.attrib.get("href", ""))
            # The OPF is never rewritten, so a reference it holds to the outside cannot be cut:
            # the EPUB is refused (a cover-image or a guide entry that points at a remote host).
            if href and not is_relative_reference(href):
                raise NeutralizationError("remote_reference", opf)
            if _local(item.tag) != "item":
                continue
            kind = _MEDIA_KIND.get(item.attrib.get("media-type", "").strip().lower())
            href = href.split("#", 1)[0]
            if kind and href:
                kinds[posixpath.normpath(posixpath.join(base, href))] = kind
    return set(opfs), kinds


def _kind_of(archive: SafeArchive, name: str, manifest: dict[str, str]) -> str | None:
    """How an entry is handled: a page kind, or None for a byte-for-byte copy.

    Every entry whose kind is not settled by its extension or its declared media type is sniffed:
    a page, an SVG or a script in a file called anything else is a document in disguise, and the
    EPUB is refused unless the file is declared as XML (then it is a page like any other).
    """
    ext = _ext(name)
    if ext in _EXT_KIND:
        return _EXT_KIND[ext]
    declared = manifest.get(name)
    if declared in ("xhtml", "svg", "css"):
        return declared
    if ext == ".svgz":  # a compressed SVG cannot be read, so it cannot be cut
        raise NeutralizationError("disguised", name)
    head = archive.read_head(name, _SNIFF).replace(b"\x00", b"")  # UTF-16 pages too
    if not _PAGE_MARK.search(head):
        return None
    if ext not in _BINARY_EXT and (declared == "sniff" or ext in _XML_LIKE_EXT):
        return "svg" if _SVG_MARK.search(head) else "xhtml"
    raise NeutralizationError("disguised", name)


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
    opfs, manifest = _manifest_kinds(archive)
    original, derived = _PayloadHash(), _PayloadHash()
    changed: list[str] = []
    with ArchiveWriter(out) as writer:
        for entry in archive.entries:
            entry_name = entry.name
            if entry.is_dir:
                writer.add(entry_name, b"")
                continue
            kind = None
            if entry_name != "mimetype" and not entry_name.startswith("META-INF/"):
                kind = None if entry_name in opfs else _kind_of(archive, entry_name, manifest)
            if kind is None:
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
