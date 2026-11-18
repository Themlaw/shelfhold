"""The KEPUB core (EF-BIB-015, M5 L20b): pure Python, no external binary.

The EPUBs are synthetic, built here: a container, an OPF with a three-document spine (the nav
included, to prove it is left alone), an NCX, a style sheet and two chapters. What is asserted
is the contract of `kepubify`: the entries that must not change do not change by one byte, the
paragraphs of the spine documents carry the Kobo spans and the book divs, the same input always
gives the same output, and an invalid, oversized or trapped EPUB is refused with the module's
own error, never with an exception of the parser or of the archive layer.
"""

import io
import random
import zipfile

import pytest

from shelfhold.infrastructure.xml import safe_parse_xml
from shelfhold.library.application.kepub import KepubError, kepubify

XHTML = "{http://www.w3.org/1999/xhtml}"

CONTAINER = b"""<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = b"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:11111111-2222-3333-4444-555555555555</dc:identifier>
    <dc:title>Synthetic Book</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="c1"/>
    <itemref idref="c2"/>
    <itemref idref="nav"/>
  </spine>
</package>
"""

NAV = b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Navigation</title></head>
<body>
<nav epub:type="toc">
  <ol>
    <li><a href="chapter1.xhtml">Chapter 1</a></li>
    <li><a href="chapter2.xhtml">Chapter 2</a></li>
  </ol>
</nav>
</body>
</html>
"""

NCX = b"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head/>
  <docTitle><text>Synthetic Book</text></docTitle>
  <navMap>
    <navPoint id="n1"><navLabel><text>Chapter 1</text></navLabel>
      <content src="chapter1.xhtml"/></navPoint>
    <navPoint id="n2"><navLabel><text>Chapter 2</text></navLabel>
      <content src="chapter2.xhtml"/></navPoint>
  </navMap>
</ncx>
"""

CSS = b"p { margin: 0 }\n"

CHAPTER1 = b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>One</title></head>
<body>
<p>First sentence. Second sentence!</p>
<p><em>Stressed</em> then plain words?</p>
</body>
</html>
"""

CHAPTER2 = b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Two</title></head>
<body>
<p>Chapter two, first line? Second line.</p>
</body>
</html>
"""


def epub(entries: dict[str, bytes]) -> bytes:
    """A synthetic EPUB: `mimetype` first and stored, every other entry deflated."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, b"application/epub+zip")
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def book(**pages: bytes) -> dict[str, bytes]:
    """The default synthetic book, with any chapter page replaced."""
    entries = {
        "META-INF/container.xml": CONTAINER,
        "OEBPS/content.opf": OPF,
        "OEBPS/nav.xhtml": NAV,
        "OEBPS/toc.ncx": NCX,
        "OEBPS/style.css": CSS,
        "OEBPS/chapter1.xhtml": CHAPTER1,
        "OEBPS/chapter2.xhtml": CHAPTER2,
    }
    entries.update(pages)
    return entries


def read_entry(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(name)


@pytest.mark.req("EF-BIB-015")
def test_kepub_keeps_spine_and_toc() -> None:
    source = book()
    result = kepubify(epub(source))
    with zipfile.ZipFile(io.BytesIO(result)) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
        # the manifest, the spine, both tables of contents and the style sheet: byte for byte
        for name in (
            "META-INF/container.xml",
            "OEBPS/content.opf",
            "OEBPS/nav.xhtml",
            "OEBPS/toc.ncx",
            "OEBPS/style.css",
        ):
            assert zf.read(name) == source[name], name
        assert set(names) == set(source) | {"mimetype"}


@pytest.mark.req("EF-BIB-015")
def test_kepub_wraps_text_in_kobo_spans() -> None:
    result = kepubify(epub(book()))
    root = safe_parse_xml(read_entry(result, "OEBPS/chapter1.xhtml"))
    spans = [
        (element.get("class"), element.get("id"), "".join(element.itertext()))
        for element in root.iter(f"{XHTML}span")
    ]
    assert spans == [
        ("koboSpan", "kobo.1.1", "First sentence. "),
        ("koboSpan", "kobo.1.2", "Second sentence!"),
        ("koboSpan", "kobo.2.1", "Stressed"),
        ("koboSpan", "kobo.2.2", " then plain words?"),
    ]
    title = next(element for element in root.iter(f"{XHTML}title"))
    assert "".join(title.itertext()) == "One"
    # the body content sits inside the two Kobo divs, in this order
    body = next(element for element in root.iter(f"{XHTML}body"))
    inner = [child for child in body if child.get("class") == "book-inner"]
    assert len(inner) == 1
    columns = [child for child in inner[0] if child.get("class") == "book-columns"]
    assert len(columns) == 1
    assert len([e for e in columns[0].iter(f"{XHTML}p")]) == 2
    # the numbering restarts on the next document of the spine
    second = safe_parse_xml(read_entry(result, "OEBPS/chapter2.xhtml"))
    ids = [e.get("id") for e in second.iter(f"{XHTML}span") if e.get("class") == "koboSpan"]
    assert ids[0] == "kobo.1.1"


@pytest.mark.req("EF-BIB-015")
def test_kepub_is_deterministic() -> None:
    data = epub(book())
    assert kepubify(data) == kepubify(data)
    with zipfile.ZipFile(io.BytesIO(kepubify(data))) as zf:
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in zf.infolist())


@pytest.mark.req("EF-BIB-015")
def test_kepub_refuses_invalid_or_oversized_epub() -> None:
    # not an archive at all
    with pytest.raises(KepubError):
        kepubify(b"this is not a zip file")
    # a zip that is not an EPUB: no container
    with pytest.raises(KepubError) as caught:
        kepubify(epub({"OEBPS/content.opf": OPF}))
    assert caught.value.reason == "no_container"
    # a spine document over the XML cap: refused before it is parsed
    big = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
        + random.Random(0).randbytes(9 * 1024 * 1024)
        + b"</p></body></html>"
    )
    with pytest.raises(KepubError) as caught:
        kepubify(epub(book(**{"OEBPS/chapter1.xhtml": big})))
    assert caught.value.reason == "entry_too_large"
    # a zip bomb: an entry whose declared size dwarfs its packed size
    bomb = book(**{"OEBPS/bomb.xhtml": b"\x00" * (2 * 1024 * 1024)})
    with pytest.raises(KepubError) as caught:
        kepubify(epub(bomb))
    assert caught.value.reason == "ratio"


@pytest.mark.req("EF-BIB-015")
def test_kepub_never_resolves_entities() -> None:
    external = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<!DOCTYPE html [<!ENTITY secret SYSTEM "file:///etc/passwd">]>\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>&secret;</p></body></html>'
    )
    laughs = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<!DOCTYPE html [<!ENTITY a "' + b"ha" * 64 + b'"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>&b;</p></body></html>'
    )
    for trapped in (external, laughs):
        with pytest.raises(KepubError) as caught:
            kepubify(epub(book(**{"OEBPS/chapter1.xhtml": trapped})))
        assert caught.value.reason == "xml_dtd"
    # a numeric character reference is not an entity: the page passes and keeps its text
    plain = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>caf&#233; au lait.</p></body></html>'
    )
    result = kepubify(epub(book(**{"OEBPS/chapter1.xhtml": plain})))
    assert "café au lait.".encode() in read_entry(result, "OEBPS/chapter1.xhtml")
