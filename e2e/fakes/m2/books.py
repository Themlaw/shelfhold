"""Synthetic EPUB files for the M2 journeys: made here, from nothing, one per title (the corpus
builder of the SDK gives the container; the package document is rewritten per book)."""

from __future__ import annotations

import hashlib

from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member, epub_bytes, opf, xhtml

__all__ = ["Member", "book", "pdf_encrypted", "pdf_plain", "volume"]


def book(
    title: str,
    *,
    author: str = "Autrice Inventée",
    lang: str = "en",
    body: str | None = None,
    metadata: str = "",
    extra: tuple[Member, ...] = (),
) -> bytes:
    """A well-formed EPUB whose identifier, title and author are its own. `metadata`: more
    elements of the package metadata (a `dc:rights`, say); `body` may be hostile on purpose."""
    ident = hashlib.sha256(title.encode()).hexdigest()[:32]
    body = (
        body if body is not None else f"<p>chapitre de {title}</p>"
    )  # its own text: a book is never the echo of another
    package = (
        opf()
        .replace(b"urn:uuid:00000000-0000-0000-0000-000000000001", f"urn:uuid:{ident}".encode())
        .replace(b"<dc:title>Synthetic</dc:title>", f"<dc:title>{title}</dc:title>".encode())
        .replace(b"<dc:language>en</dc:language>", f"<dc:language>{lang}</dc:language>".encode())
        .replace(b"</metadata>", f"<dc:creator>{author}</dc:creator>{metadata}</metadata>".encode())
    )
    return epub_bytes(xhtml(body), opf_bytes=package, extra=extra)


def pdf_plain() -> bytes:
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def pdf_encrypted() -> bytes:
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt("synthetic-owner-pass")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def volume(count: int) -> bytes:
    """A volume of `count` chapters, each one in the spine and in the table of contents (what an
    instance of the chapter crawler builds for a range)."""
    import io
    import zipfile

    container = (
        '<?xml version="1.0"?>\n<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>\n"
    )
    numbers = range(1, count + 1)
    manifest = "".join(
        f'<item id="i{n}" href="ch{n}.xhtml" media-type="application/xhtml+xml"/>' for n in numbers
    )
    refs = "".join(f'<itemref idref="i{n}"/>' for n in numbers)
    toc = "".join(f'<li><a href="ch{n}.xhtml">Chapter {n}</a></li>' for n in numbers)
    package = (
        '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="id">urn:uuid:00000000-0000-4000-8000-000000000032</dc:identifier>'
        "<dc:title>Roman synthetique</dc:title><dc:language>fr</dc:language></metadata>"
        '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        f'properties="nav"/>{manifest}</manifest><spine>{refs}</spine></package>'
    )
    nav = (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        f'<nav epub:type="toc"><ol>{toc}</ol></nav></body></html>'
    )
    members = {"META-INF/container.xml": container, "OEBPS/content.opf": package,
               "OEBPS/nav.xhtml": nav}  # fmt: skip
    for n in numbers:
        members[f"OEBPS/ch{n}.xhtml"] = (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
            f"<p>text of ch{n}.xhtml</p><script>alert({n})</script></body></html>"
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype", (2020, 1, 1, 0, 0, 0)), b"application/epub+zip"
        )
        for name, text in members.items():
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text)
    return buffer.getvalue()
