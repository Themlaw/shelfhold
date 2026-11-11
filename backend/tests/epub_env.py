"""Synthetic EPUBs for the passthrough tests: a vertical Japanese book with accessibility
metadata and a watermark-like entry, every byte made here."""

import io
import zipfile

CONTAINER = (
    '<?xml version="1.0"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles>\n'
    "</container>\n"
)

WATERMARK = bytes(range(256)) * 3  # an opaque entry that must survive untouched
RIGHTS_XML = b'<rights xmlns="urn:test"><holder>synthetic buyer 0001</holder></rights>\n'


def opf(
    *,
    language: str = "ja",
    direction: str | None = "rtl",
    version: str = "3.0",
    extra_metadata: str = "",
) -> bytes:
    spine = f' page-progression-direction="{direction}"' if direction else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="{version}" '
        'unique-identifier="id" prefix="schema: http://schema.org/">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '    <dc:identifier id="id">urn:uuid:00000000-0000-4000-8000-000000000002</dc:identifier>\n'
        "    <dc:title>Livre synthétique</dc:title>\n"
        f"    <dc:language>{language}</dc:language>\n"
        "    <dc:subject>Fiction</dc:subject>\n"
        '    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>\n'
        '    <meta property="schema:accessMode">textual</meta>\n'
        '    <meta property="schema:accessibilityFeature">tableOfContents</meta>\n'
        '    <meta property="schema:accessibilitySummary">Synthetic summary.</meta>\n'
        '    <meta name="primary-writing-mode" content="vertical-rl"/>\n'
        '    <meta property="rendition:layout">pre-paginated</meta>\n'
        f"{extra_metadata}"
        "  </metadata>\n"
        "  <manifest>\n"
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>\n'
        '    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>\n'
        "  </manifest>\n"
        f'  <spine{spine}><itemref idref="c1"/></spine>\n'
        "</package>\n"
    ).encode()


def epub(opf_bytes: bytes | None = None, *, watermark: bool = True) -> bytes:
    """`mimetype` first and stored, the rest deflated, a watermark-like entry in `META-INF`."""
    entries: list[tuple[str, bytes, int]] = [
        ("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),
        ("META-INF/container.xml", CONTAINER.encode(), zipfile.ZIP_DEFLATED),
        ("OEBPS/content.opf", opf_bytes or opf(), zipfile.ZIP_DEFLATED),
        ("OEBPS/nav.xhtml", b"<html><body><nav>x</nav></body></html>\n", zipfile.ZIP_DEFLATED),
        ("OEBPS/c1.xhtml", "<html><body><p>縦書き</p></body></html>\n".encode(), 8),
    ]
    if watermark:
        entries.insert(2, ("META-INF/rights.xml", RIGHTS_XML, zipfile.ZIP_DEFLATED))
        entries.append(("OEBPS/images/wm.bin", WATERMARK, zipfile.ZIP_STORED))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, method in entries:
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = method
            archive.writestr(info, data)
    return buffer.getvalue()
