# SPDX-License-Identifier: MIT
"""A raw ZIP and EPUB builder for hostile fixtures.

Written with `struct` and `zlib` only: the standard `zipfile` refuses to write some of the traps
(duplicate names, `../`, symbolic links) and the SDK has no business importing the core's archive
code. Output is deterministic (fixed timestamps), so a case is the same bytes on every run.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

_DOS_TIME: Final = 0
_DOS_DATE: Final = (2020 - 1980) << 9 | 1 << 5 | 1  # 2020-01-01
XHTML_NS: Final = "http://www.w3.org/1999/xhtml"
SVG_NS: Final = "http://www.w3.org/2000/svg"
XLINK_NS: Final = "http://www.w3.org/1999/xlink"
EPUB_NS: Final = "http://www.idpf.org/2007/ops"

# A 1x1 PNG, enough for the cover and any image entry.
TINY_PNG: Final = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082"
)


@dataclass(frozen=True)
class Member:
    name: str
    data: bytes
    deflate: bool = True
    attr: int = 0o644 << 16  # Unix mode in the high half, as a Unix writer stores it


def zip_bytes(members: Iterable[Member]) -> bytes:
    out = bytearray()
    central = bytearray()
    count = 0
    for member in members:
        name = member.name.encode("utf-8")
        crc = zlib.crc32(member.data)
        if member.deflate:
            packer = zlib.compressobj(9, zlib.DEFLATED, -15)
            body = packer.compress(member.data) + packer.flush()
        else:
            body = member.data
        method = 8 if member.deflate else 0
        offset = len(out)
        out += struct.pack(
            "<4s5H3L2H", b"PK\x03\x04", 20, 0x800, method, _DOS_TIME, _DOS_DATE, crc,
            len(body), len(member.data), len(name), 0,
        )  # fmt: skip
        out += name + body
        central += struct.pack(
            "<4s6H3L5H2L", b"PK\x01\x02", 0x031E, 20, 0x800, method, _DOS_TIME, _DOS_DATE, crc,
            len(body), len(member.data), len(name), 0, 0, 0, 0, member.attr, offset,
        )  # fmt: skip
        central += name
        count += 1
    directory = len(out)
    out += central
    out += struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, count, count, len(central), directory, 0)
    return bytes(out)


CONTAINER: Final = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    b'<rootfiles><rootfile full-path="OEBPS/content.opf" '
    b'media-type="application/oebps-package+xml"/></rootfiles></container>'
)
NAV: Final = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    f'<html xmlns="{XHTML_NS}" xmlns:epub="{EPUB_NS}"><head><title>Contents</title></head>'
    '<body><nav epub:type="toc"><ol><li><a href="ch1.xhtml">One</a></li></ol></nav></body></html>'
).encode()
STYLE: Final = b"body { margin: 1em; }\np { text-indent: 1em; }\n"


def opf(extra_items: str = "", guide: str = "") -> bytes:
    """A minimal EPUB 3 package document. The neutralizer must never rewrite it."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="id">urn:uuid:00000000-0000-0000-0000-000000000001</dc:identifier>'
        "<dc:title>Synthetic</dc:title><dc:language>en</dc:language></metadata><manifest>"
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="css" href="style.css" media-type="text/css"/>'
        '<item id="cover" href="cover.png" media-type="image/png"/>'
        f'{extra_items}</manifest><spine><itemref idref="ch1"/></spine>{guide}</package>'
    ).encode()


def xhtml(body: str, head: str = "", *, root_attrs: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<html xmlns="{XHTML_NS}" xmlns:epub="{EPUB_NS}" xmlns:svg="{SVG_NS}" '
        f'xmlns:xlink="{XLINK_NS}"{root_attrs}><head><title>T</title>{head}</head>'
        f"<body>{body}</body></html>"
    ).encode()


def epub_bytes(
    chapter: bytes,
    *,
    css: bytes = STYLE,
    extra: Iterable[Member] = (),
    opf_extra: str = "",
    guide: str = "",
    opf_bytes: bytes | None = None,
    first: Iterable[Member] | None = None,
) -> bytes:
    """A well-formed EPUB around `chapter`. `extra` entries come last, `first` replaces mimetype."""
    head = (
        list(first)
        if first is not None
        else [Member("mimetype", b"application/epub+zip", deflate=False)]
    )
    return zip_bytes([
        *head,
        Member("META-INF/container.xml", CONTAINER),
        Member("OEBPS/content.opf", opf_bytes or opf(opf_extra, guide)),
        Member("OEBPS/nav.xhtml", NAV),
        Member("OEBPS/ch1.xhtml", chapter),
        Member("OEBPS/style.css", css),
        Member("OEBPS/cover.png", TINY_PNG, deflate=False),
        *extra,
    ])  # fmt: skip
