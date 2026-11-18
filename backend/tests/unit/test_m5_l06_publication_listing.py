"""M5 L06 (EF-LEC-014): the listing of an archive and the direction of reading, as pure values.

What the manifest lists is the archive's own names; a package that points elsewhere (a scheme, an
absolute path, an entry that is not there), a spine with nothing to read and a format the reader
does not open are all « no readable copy », never a partial list.
"""

import pytest

from shelfhold.reading.application.content_access import NoReadableCopyError
from shelfhold.reading.application.publication import list_archive
from shelfhold.reading.domain.publication import (
    page_names,
    reading_direction,
    resolve_in_package,
)
from tests.content_env import FakeArchive
from tests.integration.test_m5_l06_publication import epub_entries

pytestmark = pytest.mark.req("EF-LEC-014")


def archive(entries: dict[str, bytes]) -> FakeArchive:
    return FakeArchive(entries, [])


def package_with(spine: str, manifest: str) -> dict[str, bytes]:
    entries = epub_entries([("a.xhtml", 5)])
    entries["OEBPS/content.opf"] = (
        "<package xmlns='http://www.idpf.org/2007/opf' version='3.0'><manifest>"
        f"{manifest}</manifest><spine>{spine}</spine></package>"
    ).encode()
    return entries


@pytest.mark.parametrize(
    ("work", "package", "kind", "expected"),
    [
        ("rtl", "ltr", "ltr", "rtl"),  # the Work's own wins
        (None, "rtl", "ltr", "rtl"),  # then the package
        (None, None, "vertical", "webtoon"),  # then the kind; `vertical` is a webtoon
        ("vertical", None, None, "webtoon"),
        (None, "default", "nonsense", "ltr"),  # nothing known: left to right
        (None, None, None, "ltr"),
    ],
)
def test_reading_direction_order(
    work: str | None, package: str | None, kind: str | None, expected: str
) -> None:
    assert reading_direction(work, package, kind) == expected


def test_pages_are_in_natural_order_and_only_images() -> None:
    names = frozenset({"p10.png", "p2.png", "p1.jpg", "notes.txt", "ComicInfo.xml", "x.js"})
    assert page_names(names) == ["p1.jpg", "p2.png", "p10.png"]


@pytest.mark.parametrize(
    "href",
    ["https://x.example/a.xhtml", "//x.example/a.xhtml", "/OEBPS/a.xhtml", "#a", "", "../../a"],
)
def test_a_package_reference_that_is_not_a_plain_name_of_the_archive_is_nothing(href: str) -> None:
    assert resolve_in_package("OEBPS/content.opf", href, frozenset({"OEBPS/a.xhtml", "a"})) is None


def test_a_reference_is_resolved_against_its_package_and_must_be_there() -> None:
    names = frozenset({"OEBPS/text/a.xhtml", "OEBPS/b.xhtml"})
    assert resolve_in_package("OEBPS/content.opf", "text/a.xhtml", names) == "OEBPS/text/a.xhtml"
    assert resolve_in_package("OEBPS/content.opf", "text/../b.xhtml", names) == "OEBPS/b.xhtml"
    assert resolve_in_package("OEBPS/content.opf", "text%2Fa.xhtml", names) == "OEBPS/text/a.xhtml"
    assert resolve_in_package("OEBPS/content.opf", "missing.xhtml", names) is None


def test_the_spine_lists_linear_documents_the_archive_holds_in_order() -> None:
    entries = package_with(
        "<itemref idref='b'/><itemref idref='gone'/><itemref idref='x'/><itemref idref='a'/>"
        "<itemref idref='side' linear='no'/>",
        "<item id='a' href='a.xhtml' media-type='application/xhtml+xml'/>"
        "<item id='b' href='nav.xhtml' media-type='application/xhtml+xml'/>"
        "<item id='x' href='https://x.example/e.xhtml' media-type='application/xhtml+xml'/>"
        "<item id='side' href='style.css' media-type='text/css'/>",
    )
    listing = list_archive(archive(entries), "epub")
    assert listing.spine == ("OEBPS/nav.xhtml", "OEBPS/a.xhtml")
    assert listing.package == "OEBPS/content.opf" and listing.kind == "epub"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.pop("META-INF/container.xml"),  # no container
        lambda e: e.__setitem__("META-INF/container.xml", b"<not xml"),
        lambda e: e.__setitem__("META-INF/container.xml", b"<container/>"),  # no rootfile
        lambda e: e.pop("OEBPS/content.opf"),  # the package it names is not there
        lambda e: e.__setitem__("OEBPS/content.opf", b"<package"),
        lambda e: e.__setitem__(  # a spine with nothing in it
            "OEBPS/content.opf", b"<package xmlns='http://www.idpf.org/2007/opf'><spine/></package>"
        ),
        lambda e: e.__setitem__(  # a DOCTYPE with an entity: refused by the safe parser
            "OEBPS/content.opf",
            b"<!DOCTYPE p [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><package>&e;</package>",
        ),
    ],
)
def test_an_epub_with_no_readable_order_is_no_readable_copy(mutate: object) -> None:
    entries = epub_entries([("a.xhtml", 5)])
    mutate(entries)  # type: ignore[operator]
    with pytest.raises(NoReadableCopyError):
        list_archive(archive(entries), "epub")


@pytest.mark.parametrize(
    ("fmt", "entries"),
    [("cbz", {"ComicInfo.xml": b"<x/>"}), ("pdf", {"b.pdf": b"%PDF"}), ("m4b", {"a.m4b": b""})],
)
def test_no_pages_and_a_format_that_is_not_opened_are_no_readable_copy(
    fmt: str, entries: dict[str, bytes]
) -> None:
    with pytest.raises(NoReadableCopyError):
        list_archive(archive(entries), fmt)
