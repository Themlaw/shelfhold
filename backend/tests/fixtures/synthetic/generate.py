"""Deterministic generator of synthetic fixtures (ENF-LEG-002).

No network, no third-party content, no real titles. Images are drawn by code,
archives use fixed timestamps and stored (uncompressed) entries so output bytes
do not depend on the zlib version. Usage: python generate.py [OUT_DIR]
(default: this directory). Writes MANIFEST.sha256 next to the files.
"""

from __future__ import annotations

import hashlib
import struct
import sys
import zipfile
import zlib
from pathlib import Path

FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def png(width: int, height: int, seed: int) -> bytes:
    """A deterministic gradient RGB PNG (stored deflate blocks)."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows += bytes(((x * 7 + seed * 40) % 256, (y * 5 + seed * 20) % 256, (x + y) % 256))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 0))
        + _chunk(b"IEND", b"")
    )


def _zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)


def comicinfo(series: str, number: int, pages: int) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<ComicInfo>\n"
        f"  <Series>{series}</Series>\n"
        f"  <Number>{number}</Number>\n"
        f"  <Title>Synthetic issue {number}</Title>\n"
        f"  <PageCount>{pages}</PageCount>\n"
        "  <LanguageISO>en</LanguageISO>\n"
        "</ComicInfo>\n"
    ).encode()


def make_cbz(path: Path, series: str, number: int, pages: int) -> None:
    entries = [(f"{i + 1:03d}.png", png(16, 24, number * 10 + i)) for i in range(pages)]
    entries.append(("ComicInfo.xml", comicinfo(series, number, pages)))
    _zip(path, entries)


def make_epub(path: Path) -> None:
    container = (
        '<?xml version="1.0"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles>\n'
        "</container>\n"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '    <dc:identifier id="id">urn:uuid:00000000-0000-4000-8000-000000000001</dc:identifier>\n'
        "    <dc:title>Synthetic Book</dc:title>\n"
        "    <dc:language>en</dc:language>\n"
        '    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>\n'
        "  </metadata>\n"
        "  <manifest>\n"
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>\n'
        '    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>\n'
        "  </manifest>\n"
        '  <spine><itemref idref="c1"/></spine>\n'
        "</package>\n"
    )
    xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        "<head><title>{t}</title></head>\n<body>{b}</body>\n</html>\n"
    )
    nav = xhtml.format(
        t="Contents",
        b='<nav epub:type="toc"><ol><li><a href="c1.xhtml">Chapter 1</a></li></ol></nav>',
    )
    chapter = xhtml.format(t="Chapter 1", b="<h1>Chapter 1</h1><p>Lorem synthetic text.</p>")
    _zip(
        path,
        [
            ("mimetype", b"application/epub+zip"),
            ("META-INF/container.xml", container.encode()),
            ("OEBPS/content.opf", opf.encode()),
            ("OEBPS/nav.xhtml", nav.encode()),
            ("OEBPS/c1.xhtml", chapter.encode()),
        ],
    )


def generate(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    make_cbz(out_dir / "comic_a_001.cbz", "Synthetic Series A", 1, 3)
    make_cbz(out_dir / "comic_a_002.cbz", "Synthetic Series A", 2, 4)
    make_epub(out_dir / "book_a.epub")
    lines = [
        f"{sha256_file(p)}  {p.name}"
        for p in sorted(out_dir.iterdir())
        if p.suffix in {".cbz", ".epub"}
    ]
    (out_dir / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    generate(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent)
