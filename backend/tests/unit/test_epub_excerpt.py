"""M5 L05a (EF-LEC-005, ADR-0055 D-P1, #361 C1): the bound of an excerpt, computed by the server."""

import io
import zipfile

import pytest

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.library.adapters.epub_excerpt import epub_excerpt
from shelfhold.reading.domain.excerpt import page_excerpt, spine_excerpt

CONTAINER = (
    b'<?xml version="1.0"?><container version="1.0" '
    b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    b'<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    b"</rootfiles></container>"
)
XHTML = "<html xmlns='http://www.w3.org/1999/xhtml'><head>{head}</head><body>{body}</body></html>"


BY_EXTENSION = {".png": "image/png", ".css": "text/css", ".woff": "font/woff"}


def epub(
    docs: dict[str, str],
    extra: dict[str, bytes] | None = None,
    types: dict[str, str] | None = None,
    spine: list[int] | None = None,
) -> bytes:
    """`extra` files are in the manifest, typed by extension unless `types` says otherwise."""
    items = "".join(
        f'<item id="d{i}" href="{n}" media-type="application/xhtml+xml"/>'
        for i, n in enumerate(docs)
    )
    for j, name in enumerate(extra or {}):
        kind = (types or {}).get(name) or BY_EXTENSION.get(name[name.rfind(".") :])
        if kind:  # no type: the file is in the archive but not in the manifest
            items += f'<item id="x{j}" href="{name.removeprefix("OEBPS/")}" media-type="{kind}"/>'
    refs = "".join(f'<itemref idref="d{i}"/>' for i in (spine or range(len(docs))))
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f"<manifest>{items}</manifest><spine>{refs}</spine></package>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        for name, text in docs.items():
            z.writestr(f"OEBPS/{name}", text)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return out.getvalue()


def cut(data: bytes) -> frozenset[str] | None:
    with safe_open_archive(io.BytesIO(data), name="b.epub") as archive:
        return epub_excerpt(archive)


def doc(size: int, body: str = "") -> str:
    return XHTML.format(head="", body=body + "<p>" + "x" * size + "</p>")


@pytest.mark.req("EF-LEC-005")
def test_epub_excerpt_is_the_first_documents_within_the_share_and_what_they_reference() -> None:
    docs = {
        "c1.xhtml": XHTML.format(
            head='<link rel="stylesheet" href="s.css"/>',
            body='<img src="img/a.png"/><a href="c2.xhtml">next</a><p>' + "x" * 100 + "</p>",
        ),
        "c2.xhtml": doc(1000),
        "c3.xhtml": doc(1000),
    }  # the first document alone is within a fifth of the three, the first two are not
    got = cut(
        epub(
            docs,
            {
                "OEBPS/img/a.png": b"png",
                "OEBPS/s.css": b"@import 'f.css'; p{background:url(bg.png)}",
                "OEBPS/f.css": b"p{}",
                "OEBPS/bg.png": b"png",
                "OEBPS/img/other.png": b"png",
            },
        )
    )
    assert got is not None
    assert {
        "OEBPS/c1.xhtml",
        "OEBPS/img/a.png",
        "OEBPS/s.css",
        "OEBPS/f.css",
        "OEBPS/bg.png",
        "OEBPS/content.opf",
        "META-INF/container.xml",
    } <= got
    assert not got & {"OEBPS/c2.xhtml", "OEBPS/img/other.png"}  # a link is not a resource


@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_epub_with_one_document_or_a_first_one_beyond_the_bound_has_no_excerpt() -> None:
    assert cut(epub({"book.xhtml": doc(50_000)})) is None  # a single document: the whole book
    assert cut(epub({"c1.xhtml": doc(5000), "c2.xhtml": doc(1000), "c3.xhtml": doc(1000)})) is None


@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_epub_excerpt_unreadable_package_or_document_is_unavailable() -> None:
    assert cut(epub({"c1.xhtml": "<html><<<", "c2.xhtml": doc(9000)})) is None
    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("a.txt", b"x")
    assert cut(broken.getvalue()) is None


@pytest.mark.req("EF-LEC-005")
def test_page_excerpt_is_ten_pages_and_a_fifth_at_most() -> None:
    pages = [f"p{i}.png" for i in range(1, 101)]
    assert page_excerpt(pages) == [f"p{i}.png" for i in range(1, 11)]  # natural order, 10 not 20
    assert page_excerpt(pages[:30]) == [f"p{i}.png" for i in range(1, 7)]  # 20 % of 30
    assert page_excerpt(pages[:4]) is None  # not even one page within the share
    assert page_excerpt([]) is None


@pytest.mark.req("EF-LEC-005")
def test_spine_excerpt_bound() -> None:
    assert spine_excerpt([("a", 10), ("b", 90)]) == ["a"]
    assert spine_excerpt([("a", 30), ("b", 70)]) is None
    assert spine_excerpt([("a", 5)]) is None
    assert spine_excerpt([]) is None


def _big(size: int) -> str:
    return doc(size)


@pytest.mark.req("EF-LEC-005")
def test_epub_excerpt_a_document_repeated_in_the_spine_is_one_document() -> None:
    assert cut(epub({"d.xhtml": _big(5000)}, spine=[0, 0, 0, 0, 0])) is None
    got = cut(epub({"a.xhtml": _big(100), "b.xhtml": _big(5000)}, spine=[0, 0, 0, 1]))
    assert got is not None
    assert "OEBPS/b.xhtml" not in got


@pytest.mark.req("EF-LEC-005")
@pytest.mark.parametrize(
    ("head", "body", "kind"),
    [
        ('<link href="r.dat"/>', "", "application/xhtml+xml"),
        ('<link rel="stylesheet" href="r.dat"/>', "", "application/xhtml+xml"),
        ("", '<img src="r.dat"/>', "application/xhtml+xml"),
        ("", '<img srcset="r.dat 2x"/>', "text/html"),
        ("", '<video poster="r.dat"/>', "image/svg+xml"),
        ("", '<object data="r.dat"/>', "application/xhtml+xml"),
        (
            "",
            '<svg xmlns:xlink="http://www.w3.org/1999/xlink"><image xlink:href="r.dat"/></svg>',
            "image/svg+xml",
        ),
        ("", '<img src="r.dat"/>', "application/octet-stream"),
        ("", '<img src="r.dat"/>', ""),  # in the archive, not in the manifest
    ],
)
def test_epub_excerpt_only_an_image_a_font_or_a_style_is_a_resource(
    head: str, body: str, kind: str
) -> None:
    docs = {"c1.xhtml": XHTML.format(head=head, body=body + "<p>x</p>"), "c2.xhtml": _big(5000)}
    got = cut(epub(docs, {"OEBPS/r.dat": b"d"}, {"OEBPS/r.dat": kind}))
    assert got is not None
    assert "OEBPS/r.dat" not in got


@pytest.mark.req("EF-LEC-005")
def test_epub_excerpt_a_style_url_to_a_document_is_not_followed() -> None:
    docs = {"c1.xhtml": doc(100, '<link rel="stylesheet" href="s.css"/>'), "c2.xhtml": _big(5000)}
    got = cut(epub(docs, {"OEBPS/s.css": b"p{background:url(c2.xhtml)}"}))
    assert got is not None
    assert "OEBPS/s.css" in got
    assert "OEBPS/c2.xhtml" not in got


@pytest.mark.req("EF-LEC-005")
def test_epub_excerpt_the_resources_together_stay_within_the_share() -> None:
    body = '<img src="a.png"/><img src="b.png"/><img src="small.png"/>'
    docs = {"c1.xhtml": doc(100, body), "c2.xhtml": _big(5000)}
    extra = {"OEBPS/a.png": b"p" * 900, "OEBPS/b.png": b"p" * 900, "OEBPS/small.png": b"p" * 10}
    got = cut(epub(docs, extra))
    assert got is not None
    assert "OEBPS/a.png" in got
    assert "OEBPS/b.png" not in got  # the share is spent
    assert "OEBPS/small.png" in got  # what still fits is served
