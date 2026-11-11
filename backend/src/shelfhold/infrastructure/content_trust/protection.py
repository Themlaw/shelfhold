"""A protected file, and a file that looks watermarked (EF-SRC-012, EF-BIB-041, ENF-LEG-009).

This module only **reads**. It says that a file is protected so that the import can refuse it with
a message and leave it where it is; it never decrypts, never strips, never asks a licence server
anything, and has no function that could (`test_never_attempts_to_lift_protection` scans for it).

Protected means, in an EPUB or any other ZIP container: a licence document (`META-INF/rights.xml`,
`META-INF/license.lcpl`) or an `META-INF/encryption.xml` that encrypts anything other than the font
obfuscation the EPUB standard defines (the two font manglings, applied to fonts only). A
licence for a loan or a purchase has its own answer, because the way out is a loan or a purchase.
In a PDF it is an `/Encrypt` entry. A file that cannot be read well enough to tell (an unreadable
`encryption.xml`) is refused as protected: the message says so, the file stays in place.

The watermark hint is a **suggestion**: it sets a flag that only makes the file more restricted
(never in a diagnostic bundle, never to an external egress); it never exempts an `untrusted`
EPUB from the neutralizer, and it never refuses a file.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from shelfhold.infrastructure.archives import (
    DEFAULT_LIMITS,
    ArchiveLimits,
    SafeArchive,
    safe_open_archive,
)
from shelfhold.infrastructure.content_trust.xhtml_neutralizer import (
    XML_LIMITS_INSPECT,
    NeutralizationError,
    parse_xml_document,
)

PROTECTED: Final = "import.protected"
PROTECTED_LICENCE: Final = "import.protected_licence"

# The font obfuscations of the EPUB standard: a mangling of the first bytes of a font, no secret.
FONT_OBFUSCATION: Final = frozenset(
    {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
)
_FONT_EXTENSIONS: Final = (".ttf", ".otf", ".woff", ".woff2", ".ttc")
_LICENCE_MARK: Final = "readium.org/2014/01/lcp"
_LICENCE_ENTRIES: Final = ("meta-inf/rights.xml", "meta-inf/license.lcpl")
_ENCRYPTION: Final = "meta-inf/encryption.xml"
_PDF_ENCRYPT = re.compile(rb"/Encrypt\s*(?:\d+\s+\d+\s+R|<<)")
_CHUNK: Final = 1 << 20


@dataclass(frozen=True)
class Protection:
    code: str  # `PROTECTED` or `PROTECTED_LICENCE`
    reason: str  # a closed word, for the log: never an entry name or a file's text


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _by_lower(archive: SafeArchive) -> dict[str, str]:
    return {e.name.lower(): e.name for e in archive.entries if not e.is_dir}


def _encryption_verdict(archive: SafeArchive, name: str) -> Protection | None:
    try:
        root = parse_xml_document(
            bytes(archive.read(name, max_bytes=1_000_000)), XML_LIMITS_INSPECT
        )
    except (NeutralizationError, OSError, KeyError, ValueError):
        return Protection(PROTECTED, "encryption_unreadable")
    protected = False
    for element in root.iter():
        if _local(element.tag) != "EncryptedData":
            continue
        algorithm = ""
        uri = ""
        for child in element.iter():
            if _local(child.tag) == "EncryptionMethod":
                algorithm = child.get("Algorithm", "")
            elif _local(child.tag) == "CipherReference":
                uri = child.get("URI", "")
        if _LICENCE_MARK in _text_of(element):
            return Protection(PROTECTED_LICENCE, "licence_encryption")
        if algorithm in FONT_OBFUSCATION and uri.lower().split("?")[0].endswith(_FONT_EXTENSIONS):
            continue  # a mangled font: not a protection
        protected = True
    for element in root.iter():  # the licence is named in the key info, wherever it sits
        if _LICENCE_MARK in " ".join(str(v) for v in element.attrib.values()):
            return Protection(PROTECTED_LICENCE, "licence_encryption")
    return Protection(PROTECTED, "encrypted_content") if protected else None


def _text_of(element: object) -> str:
    """Every attribute value and text under an element, for the one marker we look for."""
    parts: list[str] = []
    for node in element.iter():  # type: ignore[attr-defined]
        parts.extend(str(v) for v in node.attrib.values())
        if node.text:
            parts.append(node.text)
    return " ".join(parts)


def zip_protection(
    path: Path, kind: str = "cbz", limits: ArchiveLimits = DEFAULT_LIMITS
) -> Protection | None:
    """Protection of an EPUB (or of any ZIP container: a licence document in a comic archive is
    the same thing). Raises `UnsafeArchiveError` for an archive that is not safe to open."""
    with safe_open_archive(path, name=f"import.{kind}", limits=limits) as archive:
        names = _by_lower(archive)
        if "meta-inf/license.lcpl" in names:
            return Protection(PROTECTED_LICENCE, "licence_document")
        if "meta-inf/rights.xml" in names:
            return Protection(PROTECTED, "rights_document")
        if _ENCRYPTION in names:
            return _encryption_verdict(archive, names[_ENCRYPTION])
    return None


def pdf_protection(path: Path) -> Protection | None:
    """An `/Encrypt` entry anywhere in the file (the trailer, or the dictionary of a cross-reference
    stream), read in blocks to the end: padding cannot push it out of the window."""
    carry = b""
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            if _PDF_ENCRYPT.search(carry + chunk):
                return Protection(PROTECTED, "pdf_encrypted")
            carry = chunk[-32:]
    return None


def detect_protection(
    path: Path, kind: str, limits: ArchiveLimits = DEFAULT_LIMITS
) -> Protection | None:
    """`kind` is what the bytes are (`epub`, `cbz`, `pdf`): never what the file is called."""
    if kind == "pdf":
        return pdf_protection(path)
    if kind in ("epub", "cbz"):
        return zip_protection(path, kind, limits)
    return None


# -- the watermark hint ------------------------------------------------------------------------

_STANDARD_META_INF: Final = frozenset(
    {
        "meta-inf/container.xml", "meta-inf/metadata.xml", "meta-inf/manifest.xml",
        "meta-inf/signatures.xml", "meta-inf/com.apple.ibooks.display-options.xml",
        "meta-inf/calibre_bookmarks.txt",
    }
)  # fmt: skip
_PERSONALISED = re.compile(
    rb"(?:licen[cs]ed|purchased|bought|personali[sz]ed|downloaded|ordered)\s+(?:to|by|for)\b"
    rb"|water-?mark",
    re.I,
)
_WATERMARK_NAME = re.compile(r"water-?mark", re.I)
_OPF_MAX = 2_000_000


def watermark_hint(path: Path, kind: str, limits: ArchiveLimits = DEFAULT_LIMITS) -> str | None:
    """A closed word naming why the file looks watermarked, or None. A guess from entries and
    metadata that typically carry a personalised mark: a suggestion, never a verdict."""
    if kind not in ("epub", "cbz"):
        return None
    with safe_open_archive(path, name=f"import.{kind}", limits=limits) as archive:
        names = [e.name for e in archive.entries if not e.is_dir]
        if any(_WATERMARK_NAME.search(n) for n in names):
            return "entry_name"
        if kind == "cbz":
            return None
        for name in names:
            lowered = name.lower()
            if (
                lowered.startswith("meta-inf/")
                and lowered not in _STANDARD_META_INF
                and lowered not in (_ENCRYPTION, *_LICENCE_ENTRIES)
            ):
                return "extra_meta_inf_entry"
        for name in names:
            if name.lower().endswith(".opf"):
                try:
                    opf = bytes(archive.read(name, max_bytes=_OPF_MAX))
                except (OSError, KeyError, ValueError):
                    continue
                if _PERSONALISED.search(opf):
                    return "personalised_metadata"
    return None
