"""The second, independent look at the KEPUB core (EF-BIB-015, M5 L20b).

Every test here pins one hole an adversarial pass over `kepub.py` found; each is red and
`xfail(strict=True)`, and must be flipped to a plain green test the day the hole is closed.
The EPUBs are synthetic, built here. The five holes, in order:

1. the `kobo.N.M` ids the spans receive can collide with an id the page already carries —
   a duplicate id breaks every anchor and every reading position (CFI) that points at it;
2. an `<itemref>` found anywhere in the OPF counts as spine, so an entry that is not in the
   reading order is rewritten although the module copies it byte for byte;
3. a carriage return read from `&#13;` is written back as a literal CR, which no XML reader
   reads back as a CR: a literal CR in text is normalized to LF on the way in, and only the
   reference form survives a parse — `_esc_attr` escapes it, `_esc_text` does not;
4. the comments and processing instructions of a spine document are dropped on the way, so
   what the reader gets is not the document the parser read;
5. every segment of one text node is inserted with its own `insert` in front of the other
   children of its holder, and each insert shifts them all: one paragraph with S segments and
   K children costs S*K pointer moves, well within the caps the module accepts.
"""

import io
import time
import zipfile

import pytest

from shelfhold.infrastructure.xml import safe_parse_xml
from shelfhold.library.application.kepub import kepubify

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
    <item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="extra" href="extra.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
  </manifest>
  <spine>
    <itemref idref="c1"/>
  </spine>
</package>
"""

# the same package, with the extra page referenced from nowhere the spine reaches
OPF_STRAY = OPF.replace(b"</metadata>", b'    <itemref idref="extra"/>\n  </metadata>')

CSS = b"p { margin: 0 }\n"


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


def read_entry(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(name)


def book(chapter1: bytes, opf: bytes = OPF, extra: bytes | None = None) -> bytes:
    entries = {
        "META-INF/container.xml": CONTAINER,
        "OEBPS/content.opf": opf,
        "OEBPS/style.css": CSS,
        "OEBPS/chapter1.xhtml": chapter1,
    }
    if extra is not None:
        entries["OEBPS/extra.xhtml"] = extra
    return epub(entries)


def page(body: bytes) -> bytes:
    return (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body>' + body + b"</body></html>"
    )


@pytest.mark.req("EF-BIB-015")
@pytest.mark.xfail(strict=True)
def test_kepub_span_ids_never_collide_with_existing_ones() -> None:
    """A span id must not duplicate an id the page already carries: `kobo.1.1` on the
    paragraph becomes two elements with the same id once the first span is numbered, and
    every anchor (a table of contents link, a footnote) and every reading position that
    points at it is ambiguous from then on. A page already converted once carries these
    ids already, so a second pass meets the collision on the first paragraph."""
    result = kepubify(book(page(b'<p id="kobo.1.1">First sentence. Second.</p>')))
    root = safe_parse_xml(read_entry(result, "OEBPS/chapter1.xhtml"))
    ids = [element.get("id") for element in root.iter() if element.get("id")]
    assert "kobo.1.1" in ids  # the paragraph kept its id
    assert any(element.get("class") == "koboSpan" for element in root.iter())
    assert len(ids) == len(set(ids)), f"duplicate ids in the page: {sorted(ids)}"


@pytest.mark.req("EF-BIB-015")
@pytest.mark.xfail(strict=True)
def test_kepub_transforms_only_the_itemrefs_of_the_spine() -> None:
    """The reading order is what the `<spine>` of the OPF references. An `<itemref>` that
    sits in `<metadata>` is not part of it: the entry it names must stay byte for byte,
    like every entry the module does not transform. The scan reads any element whose local
    name is `itemref`, wherever it lies, so the extra page gets the spans too."""
    chapter = page(b"<p>Chapter.</p>")
    extra = page(b"<p>Not in the reading order.</p>")
    result = kepubify(book(chapter, opf=OPF_STRAY, extra=extra))
    # sanity: the spine document was transformed, the refusal is not "nothing happened"
    assert b"koboSpan" in read_entry(result, "OEBPS/chapter1.xhtml")
    assert read_entry(result, "OEBPS/extra.xhtml") == extra


@pytest.mark.req("EF-BIB-015")
@pytest.mark.xfail(strict=True)
def test_kepub_keeps_a_carriage_reference_through_the_round_trip() -> None:
    """What the safe parser read must be what the written document says. The parser hands
    `&#13;` over as one carriage return; the writer puts that CR into the text literally,
    and a literal CR in text is a character no XML reader will read back (each one
    normalizes it to LF): the book the device receives says something else than the book
    that was parsed. The writer knows the trick — `_esc_attr` writes `&#13;` — but for
    text no escaping of CR happens."""
    source = page(b"<p>before&#13;after. Tail.</p>")
    assert "\r" in "".join(safe_parse_xml(source).itertext())  # the parser keeps the CR
    result = kepubify(book(source))
    root = safe_parse_xml(read_entry(result, "OEBPS/chapter1.xhtml"))
    assert "\r" in "".join(root.itertext())


@pytest.mark.req("EF-BIB-015")
@pytest.mark.xfail(strict=True)
def test_kepub_keeps_the_comments_and_instructions_of_a_page() -> None:
    """A comment or a processing instruction inside the body belongs to the document the
    parser reads; the written page has neither. A stylesheet instruction lost this way
    changes the rendering of the book on the device, silently."""
    source = page(
        b'<!-- a comment that matters --><p>one.</p><?an-instruction rules="yes"?><p>two.</p>'
    )
    result = kepubify(book(source))
    written = read_entry(result, "OEBPS/chapter1.xhtml")
    assert b"koboSpan" in written  # sanity: the page was transformed
    assert b"a comment that matters" in written
    assert b"an-instruction" in written


@pytest.mark.req("EF-BIB-015")
@pytest.mark.xfail(strict=True)
def test_kepub_stays_linear_on_a_paragraph_of_many_segments() -> None:
    """Each segment of one text node becomes one span, inserted with its own `insert` in
    front of the other children of its holder: every insert shifts them all, so a
    paragraph with S segments of its own text and K children that follow costs S*K
    pointer moves. Both S and K fit the caps the module accepts (8 MiB, 100 000 elements),
    and doubling both must not quadruple the time: within one order of the caps the page
    below already costs tens of seconds where a linear transform needs one."""

    def hostile(spans: int, children: int) -> bytes:
        return page(b"<p>" + b". " * spans + b"<i/>" * children + b"</p>")

    small = book(hostile(175_000, 35_000))
    big = book(hostile(350_000, 70_000))
    start = time.perf_counter()
    kepubify(small)
    small_seconds = time.perf_counter() - start
    start = time.perf_counter()
    kepubify(big)
    big_seconds = time.perf_counter() - start
    ratio = big_seconds / small_seconds
    assert ratio < 3.0, (
        f"quadratic on one paragraph: {small_seconds:.1f} s then {big_seconds:.1f} s"
        f" (x{ratio:.1f} for twice the segments and twice the children)"
    )
