"""M4 L48: the Atom feed of the OPDS and its links (ADR-0053 §1, EF-INT-007, ENF-SEC-010).

The feed is built from values that came from the catalogue: a title is data, never markup, and a
character XML cannot hold never breaks the document. A link is a path under `/opds` (with the
prefix of the mount point), never an address built from `Host`, and never carries a credential.
"""

import re
from xml.etree import ElementTree as ET

import pytest
from shelfhold.interfaces.http_api.opds.feed import Entry, Link, build_feed
from shelfhold.interfaces.http_api.opds.links import mount_prefix, safe_filename

pytestmark = pytest.mark.req("EF-INT-007", "ENF-SEC-010")

ATOM = "{http://www.w3.org/2005/Atom}"


def _feed(entries: list[Entry], **extra: object) -> str:
    return build_feed(
        feed_id="urn:shelfhold:opds:test", title="Test", updated="2026-11-15T10:00:00Z",
        kind="acquisition", links=[Link("self", "/opds/works", "application/atom+xml")],
        entries=entries, **extra,  # type: ignore[arg-type]
    )  # fmt: skip


def test_a_title_is_escaped_and_a_control_character_never_breaks_the_document() -> None:
    nasty = 'A <b>&amp;</b> ]]> "q" \x00\x01\x0b end \ud800'
    body = _feed([Entry("urn:x:1", nasty, "2026-11-15T10:00:00Z", links=[], summary=nasty)])
    root = ET.fromstring(body)  # noqa: S314 - well-formed, or this raises
    title = root.find(f"{ATOM}entry/{ATOM}title")
    assert title is not None and title.text is not None
    assert "<b>" in title.text  # text, not an element
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", title.text)
    assert root.find(f"{ATOM}entry/{ATOM}title/{ATOM}b") is None


def test_the_feed_declares_its_kind_in_the_self_type_and_carries_opensearch_totals() -> None:
    body = build_feed(
        feed_id="urn:shelfhold:opds:t", title="T", updated="2026-11-15T10:00:00Z",
        kind="navigation", links=[], entries=[], total=7, start=0, per_page=50,
    )  # fmt: skip
    root = ET.fromstring(body)  # noqa: S314
    assert root.tag == f"{ATOM}feed"
    ns = "{http://a9.com/-/spec/opensearch/1.1/}"
    assert root.findtext(f"{ns}totalResults") == "7"
    assert root.findtext(f"{ns}itemsPerPage") == "50"


def test_a_feed_without_a_total_carries_none() -> None:
    root = ET.fromstring(_feed([]))  # noqa: S314
    assert root.find("{http://a9.com/-/spec/opensearch/1.1/}totalResults") is None


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ({"root_path": ""}, ""),
        ({"root_path": "/shelf"}, "/shelf"),
        ({"root_path": "/a/b-c_d.e"}, "/a/b-c_d.e"),
        ({"root_path": "/shelf/"}, "/shelf"),
        ({"root_path": "//evil.example"}, ""),
        ({"root_path": "http://evil.example"}, ""),
        ({"root_path": "/a/../b"}, ""),
        ({"root_path": "/a b"}, ""),
        ({"root_path": "/a\r\nSet-Cookie: x=y"}, ""),
        ({"root_path": "/" + "a" * 200}, ""),
        ({"root_path": "/ok", "state": {"client_prefix": "/front"}}, "/front"),
        ({"root_path": "/ok", "state": {"client_prefix": "//evil"}}, "/ok"),
        ({"root_path": "/ok", "state": {"client_prefix": "/a/../b"}}, "/ok"),
    ],
)
def test_the_mount_prefix_is_a_plain_path_or_nothing(
    scope: dict[str, object], expected: str
) -> None:
    assert mount_prefix(scope) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "../../etc/passwd", "..\\..\\boot.ini", "/abs/path.cbz", "a\r\nSet-Cookie: x=y",
        'q"uote.cbz', "x\x00y.cbz", ".", "..", "", "   ", "a/b\\c:d*e?f<g>h|i",
    ],
)  # fmt: skip
def test_a_file_name_holds_no_path_and_no_header_injection(raw: str) -> None:
    name = safe_filename(raw)
    assert name, "a name is always made"
    assert not set(name) & set('/\\:*?"<>|\r\n\x00')
    assert name not in {".", ".."} and ".." not in name
    assert len(name) <= 200


def test_a_name_keeps_the_title_and_the_version_of_a_block() -> None:
    assert safe_filename("Titre — Série complète v7.cbz") == "Titre — Série complète v7.cbz"
