"""The KEPUB core (EF-BIB-015, M5 L20b): pure Python, no external binary.

The EPUBs are synthetic, built here: a container, an OPF with a three-document spine (the nav
included, to prove it is left alone), an NCX, a style sheet and two chapters. What is asserted
is the contract of `kepubify`: the entries that must not change do not change by one byte, the
paragraphs of the spine documents carry the Kobo spans and the book divs, the same input always
gives the same output, and an invalid, oversized or trapped EPUB is refused with the module's
own error, never with an exception of the parser or of the archive layer.
"""

import hashlib
import io
import time
import zipfile

import pytest

from shelfhold.infrastructure.xml import Element, safe_parse_xml
from shelfhold.library.application.kepub import KepubError, _Serializer, kepubify

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
        + hashlib.shake_256(b"oversized spine document").digest(9 * 1024 * 1024)
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
        b'<!DOCTYPE html [<!ENTITY a "'
        + b"ha" * 64
        + b'">'
        + b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>&b;</p></body></html>'
    )
    for trapped in (external, laughs):
        with pytest.raises(KepubError) as caught:
            kepubify(epub(book(**{"OEBPS/chapter1.xhtml": trapped})))
        assert caught.value.reason == "xml_dtd"
    # a numeric character reference is not an entity: the page passes and keeps its text
    plain = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>th&#233; au lait.</p></body></html>'
    )
    result = kepubify(epub(book(**{"OEBPS/chapter1.xhtml": plain})))
    assert "thé au lait.".encode() in read_entry(result, "OEBPS/chapter1.xhtml")


MIXED = b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"
      xmlns:xlink="http://www.w3.org/1999/xlink"
      xmlns:mine="urn:shelfhold:test">
<head><title>Mixed</title></head>
<body>
<p epub:type="chapter">A page with a drawing
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"><image xlink:href="pic.png"/></svg>
  and a bare <plain xmlns="">no-namespace</plain> word.</p>
<p mine:note="kept">A foreign <mine:note>element</mine:note> too.</p>
</body>
</html>
"""

_ADDED = ("koboSpan", "book-inner", "book-columns")  # what the transform itself adds


def _shape(root: Element) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    """Every element as (tag, attributes), the added spans and divs left out."""
    return [
        (element.tag, tuple(sorted(element.attrib.items())))
        for element in root.iter()
        if element.get("class") not in _ADDED
    ]


@pytest.mark.req("EF-BIB-015")
def test_kepub_round_trip_keeps_every_namespace() -> None:
    """A page that mixes namespaces goes through and comes back tag for tag: an element with
    no namespace must not become an XHTML one on the way (it needs `xmlns=""`)."""
    result = kepubify(epub(book(**{"OEBPS/chapter1.xhtml": MIXED})))
    before = safe_parse_xml(MIXED)
    after = safe_parse_xml(read_entry(result, "OEBPS/chapter1.xhtml"))
    assert _shape(after) == _shape(before)
    assert "".join(after.itertext()) == "".join(before.itertext())
    # the bare element is still in no namespace, not in the XHTML default one
    bare = [e for e in after.iter() if not e.tag.startswith("{")]
    assert [e.tag for e in bare] == ["plain"]
    assert "".join(bare[0].itertext()) == "no-namespace"


@pytest.mark.req("EF-BIB-015")
def test_kepub_refuses_characters_xml_cannot_write() -> None:
    # through the entry point: a reference to a character XML 1.0 forbids is refused by the parser
    for reference in (b"&#0;", b"&#11;", b"&#12;", b"&#31;", b"&#xFFFE;"):
        page = (
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>before '
            + reference
            + b" after</p></body></html>"
        )
        with pytest.raises(KepubError) as caught:
            kepubify(epub(book(**{"OEBPS/chapter1.xhtml": page})))
        assert caught.value.reason.startswith("xml_")
    # the writer itself refuses, in text and in attributes, rather than emitting a document
    # no reader accepts: the parser can never hand it such a character, this is the backstop
    for bad in ("\x00", "\x0b", "\x0c", "\x1f", "￾", "\ud800"):
        element = Element(f"{XHTML}p", {})
        element.text = f"before {bad} after"
        with pytest.raises(KepubError) as caught:
            _Serializer().document(element)
        assert caught.value.reason == "character"
    element = Element(f"{XHTML}p", {"title": "a\x0cb"})
    with pytest.raises(KepubError):
        _Serializer().document(element)


@pytest.mark.req("EF-BIB-015")
def test_kepub_accepts_a_simple_doctype_and_refuses_any_other() -> None:
    # the common form of EPUB 3: a bare declaration, no subset, no external identifier
    simple = (
        b"<!DOCTYPE html>\n"
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        b"<p>With a doctype. Twice.</p></body></html>"
    )
    written = read_entry(
        kepubify(epub(book(**{"OEBPS/chapter1.xhtml": simple}))), "OEBPS/chapter1.xhtml"
    )
    assert b"koboSpan" in written
    # an internal subset can declare an entity: it stays in the bytes and the parser refuses it
    subset = (
        b'<!DOCTYPE html [<!ENTITY x "y">]>\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>&x;</p></body></html>'
    )
    with pytest.raises(KepubError) as caught:
        kepubify(epub(book(**{"OEBPS/chapter1.xhtml": subset})))
    assert caught.value.reason == "xml_dtd"
    # an external identifier points the parser at a DTD of its own: refused too
    external = (
        b'<!DOCTYPE html SYSTEM "book.dtd">\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Text.</p></body></html>'
    )
    with pytest.raises(KepubError) as caught:
        kepubify(epub(book(**{"OEBPS/chapter1.xhtml": external})))
    assert caught.value.reason == "xml_dtd"


@pytest.mark.req("EF-BIB-015")
def test_kepub_refuses_a_manifest_that_names_one_id_twice() -> None:
    opf = OPF.replace(
        b'<item id="css"',
        b'<item id="c1" href="other.xhtml" media-type="application/xhtml+xml"/>\n    '
        b'<item id="css"',
    )
    assert opf != OPF  # the edit landed
    with pytest.raises(KepubError) as caught:
        kepubify(epub(book(**{"OEBPS/content.opf": opf})))
    assert caught.value.reason == "manifest"


@pytest.mark.req("EF-BIB-015")
def test_kepub_leaves_a_page_it_cannot_rewrite_without_loss() -> None:
    # already converted once: a second pass must not number the spans again
    converted = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><div class="book-inner">'
        b'<div class="book-columns"><p><span class="koboSpan" id="kobo.1.1">Once.</span></p>'
        b"</div></div></body></html>"
    )
    result = kepubify(epub(book(**{"OEBPS/chapter1.xhtml": converted})))
    assert read_entry(result, "OEBPS/chapter1.xhtml") == converted
    # the safe parser drops comments and processing instructions: a page that carries one is
    # left as it is, not rewritten without them
    commented = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><!-- keep me --><p>Text.</p>'
        b"<?keep me too?></body></html>"
    )
    result = kepubify(epub(book(**{"OEBPS/chapter1.xhtml": commented})))
    assert read_entry(result, "OEBPS/chapter1.xhtml") == commented


@pytest.mark.req("EF-BIB-015")
def test_kepub_transform_time_follows_the_size_of_the_page() -> None:
    """No absolute threshold (a shared machine has no stable second): the same work on four
    times the page must stay under six times the time, which a transform that rebuilds each
    list of children once passes and a transform that inserts each span on its own does not."""

    def hostile(segments: int, children: int) -> bytes:
        return (
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
            + b". " * segments
            + b"<i/>" * children
            + b"</p></body></html>"
        )

    small = epub(book(**{"OEBPS/chapter1.xhtml": hostile(20_000, 5_000)}))
    big = epub(book(**{"OEBPS/chapter1.xhtml": hostile(80_000, 20_000)}))

    def best_of_three(book_bytes: bytes) -> float:
        # the fastest of three runs: a parallel suite's neighbours only ever add time
        runs = []
        for _ in range(3):
            start = time.perf_counter()
            kepubify(book_bytes)
            runs.append(time.perf_counter() - start)
        return min(runs)

    small_seconds = best_of_three(small)
    big_seconds = best_of_three(big)
    assert big_seconds < 6 * small_seconds, (small_seconds, big_seconds)
