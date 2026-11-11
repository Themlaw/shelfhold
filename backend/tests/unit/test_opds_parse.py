"""M2 L22 (EF-SRC-006, EF-SRC-023, ENF-SEC-014): the OPDS 1.2 (Atom) and 2.0 (JSON) readers and
the OpenSearch templates. The documents are synthetic, in the shape Kavita and Komga serve them.
The parsers read hostile input: each "never" below is a test that tries to break it."""

from __future__ import annotations

import json

import pytest
from shelfhold.plugin_host.adapters.native.opds import atom, opensearch
from shelfhold.plugin_host.adapters.native.opds import json as opds_json
from shelfhold.plugin_host.adapters.native.opds.model import Feed, FeedError

from shelfhold.infrastructure.xml import safe_parse_xml
from tests.fakes.opds import (
    ATOM,
    OPDS2,
    OPENSEARCH,
    acquisition_entry,
    atom_feed,
    entry_document,
    kavita_description,
    komga_description,
    navigation_entry,
    opds2_feed,
    publication,
)
from tests.fakes.opds.feeds import BUY, CBZ, EPUB, OPEN_ACCESS, link

BASE = "https://opds.example.org/opds/"


def read_atom(body: bytes, url: str = BASE) -> Feed:
    return atom.parse_atom(body, url, safe_parse_xml)


def read_json(body: bytes, url: str = BASE) -> Feed:
    return opds_json.parse_opds2(body, url)


# -- OPDS 1.2 and 2.0 ------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-006")
def test_opds_1_2_and_2_0_parsed() -> None:
    kavita = atom_feed(
        "urn:test:series",
        "Résultats",
        [
            navigation_entry("urn:test:series:7", "Une série", "/opds/series/7"),
            acquisition_entry("urn:test:book:1", "Un livre", "/opds/files/1.epub", EPUB),
        ],
        links=[
            link("self", "/opds/series?query=a", ATOM),
            link("next", "/opds/series?query=a&page=2", ATOM),
            link("search", "/opds/search", OPENSEARCH),
        ],
    )
    feed = read_atom(kavita, f"{BASE}series?query=a")
    assert [e.id for e in feed.entries] == ["urn:test:series:7", "urn:test:book:1"]
    series, book = feed.entries
    assert series.subsection == "https://opds.example.org/opds/series/7"
    assert series.acquisitions == ()
    (acquisition,) = book.acquisitions
    assert acquisition.href == "https://opds.example.org/opds/files/1.epub"  # resolved, absolute
    assert acquisition.type == EPUB
    assert book.title == "Un livre" and book.authors == ("Autrice Inventée",)
    assert book.language == "fr" and book.summary == "Un résumé inventé."
    assert feed.next == "https://opds.example.org/opds/series?query=a&page=2"
    assert feed.search is not None and feed.search.type == OPENSEARCH
    assert feed.kind == "atom"

    komga = atom_feed(
        "urn:komga:books",
        "Livres",
        [
            acquisition_entry(
                "urn:komga:b1",
                "Tome 1",
                "/opds/v1.2/books/b1/file/tome-1.cbz",
                CBZ,
                rel=OPEN_ACCESS,
            )
        ],
    )
    (tome,) = read_atom(komga).entries
    assert tome.acquisitions[0].type == CBZ

    v2 = opds2_feed(
        "Catalogue",
        publications=[publication("pub-1", "Un livre 2.0", "/opds/v2/files/pub-1.epub")],
        links=[
            {"rel": "self", "href": "/opds/v2", "type": OPDS2},
            {"rel": "next", "href": "/opds/v2?page=2", "type": OPDS2},
            {"rel": "search", "href": "/opds/v2/search{?query}", "type": OPDS2, "templated": True},
        ],
    )
    feed2 = read_json(v2, f"{BASE}v2")
    assert feed2.kind == "opds2"
    (pub,) = feed2.entries
    assert (
        pub.id == "pub-1" and pub.title == "Un livre 2.0" and pub.authors == ("Autrice Inventée",)
    )
    assert pub.acquisitions[0].href == "https://opds.example.org/opds/v2/files/pub-1.epub"
    assert feed2.next == "https://opds.example.org/opds/v2?page=2"
    assert feed2.search is not None and feed2.search.templated

    nav = opds2_feed("Racine", navigation=[{"title": "Séries", "href": "/opds/v2/series"}])
    (entry,) = read_json(nav).entries
    assert entry.subsection == "https://opds.example.org/opds/v2/series"


@pytest.mark.req("EF-SRC-006")
def test_opds_acquisition_kinds_are_the_free_files_only() -> None:
    """A purchase or a sample is not a free file; a PDF or audio is not a file the host takes."""
    entry = atom_feed(
        "urn:x",
        "x",
        [
            acquisition_entry(
                "urn:b",
                "Livre",
                "/f/free.epub",
                EPUB,
                extra=[
                    link(BUY, "/f/buy.epub", EPUB),
                    link("http://opds-spec.org/acquisition/sample", "/f/sample.epub", EPUB),
                    link(OPEN_ACCESS, "/f/book.pdf", "application/pdf"),
                    link(OPEN_ACCESS, "/f/book.mp3", "audio/mpeg"),
                ],
            )
        ],
    )
    (parsed,) = read_atom(entry).entries
    assert [a.href.rsplit("/", 1)[-1] for a in parsed.acquisitions] == ["free.epub"]
    assert parsed.locked is False
    only_buy = atom_feed(
        "urn:x", "x", [acquisition_entry("urn:c", "Payant", "/f/buy.epub", EPUB, rel=BUY)]
    )
    (paid,) = read_atom(only_buy).entries
    assert paid.acquisitions == () and paid.locked is True


@pytest.mark.req("EF-SRC-006", "ENF-SEC-014")
def test_opds_links_with_other_schemes_are_dropped() -> None:
    """Attack: a feed lists `file:`, `javascript:`, `data:` and `ftp:` links: none is kept."""
    hostile = ["file:///etc/passwd", "javascript:alert(1)", "data:text/plain,x", "ftp://h/x.epub"]
    entries = [acquisition_entry(f"urn:e{i}", "x", href, EPUB) for i, href in enumerate(hostile)]
    feed = read_atom(atom_feed("urn:x", "x", entries))
    assert all(e.acquisitions == () for e in feed.entries)
    pubs = [publication(f"p{i}", "x", href) for i, href in enumerate(hostile)]
    assert all(e.acquisitions == () for e in read_json(opds2_feed("x", publications=pubs)).entries)


@pytest.mark.req("ENF-SEC-014")
def test_opds_xml_with_entities_or_a_dtd_is_refused() -> None:
    """Attack: billion laughs, an external entity, a DTD: refused before any expansion."""
    laughs = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&b;</title></feed>'
    )
    external = (
        b'<?xml version="1.0"?><!DOCTYPE f [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&x;</title></feed>'
    )
    for body in (laughs, external):
        with pytest.raises(FeedError):
            read_atom(body)


@pytest.mark.req("ENF-SEC-014")
def test_opds_hostile_documents_are_refused_not_crashed() -> None:
    """Attack: not a feed, malformed, deeply nested, huge, JSON that is a list or a scalar."""
    for body in (b"", b"<html><body>hi</body></html>", b"<feed", b"\x00\x01\x02"):
        with pytest.raises(FeedError):
            read_atom(body)
    deep = b"<feed xmlns='http://www.w3.org/2005/Atom'>" + b"<a>" * 500 + b"</a>" * 500 + b"</feed>"
    with pytest.raises(FeedError):
        read_atom(deep)
    for body in (b"", b"[]", b"3", b"null", b'{"metadata": 3}', b"{" * 2000 + b"}" * 2000):
        with pytest.raises(FeedError):
            read_json(body)
    many = atom_feed(
        "urn:x", "x", [acquisition_entry(f"urn:{i}", "x", f"/f/{i}.epub") for i in range(5000)]
    )
    assert len(read_atom(many).entries) <= atom.MAX_ENTRIES  # a feed is read up to a bound


@pytest.mark.req("EF-SRC-006", "ENF-SEC-014")
def test_opds_text_is_read_as_text_never_as_markup() -> None:
    """A summary or title that holds markup stays text: nothing is interpreted, nothing is lost."""
    markup = '<script>alert(1)</script> & "quoted"'
    feed = read_atom(
        atom_feed(
            "urn:x", markup, [acquisition_entry("urn:b", markup, "/f/b.epub", summary=markup)]
        )
    )
    (entry,) = feed.entries
    assert entry.title == markup and entry.summary == markup


# -- the entry as received ------------------------------------------------------------------


@pytest.mark.req("EF-SRC-006")
def test_opds_entry_stored_as_received_same_hash() -> None:
    """The same entry, met in a search feed and served alone, has the same digest (what the host
    keeps as the revision hint); any change of what the source says changes it."""
    raw = acquisition_entry("urn:b", "Un livre", "/opds/f/b.epub", EPUB, summary="  espaces  \n")
    in_feed = read_atom(atom_feed("urn:s", "Résultats", [raw])).entries[0]
    alone = atom.parse_atom(entry_document(raw), f"{BASE}b", safe_parse_xml)
    (single,) = alone.entries
    assert alone.is_entry and in_feed.digest == single.digest
    assert in_feed.summary == "  espaces  \n"  # as received, not stripped
    assert in_feed.digest.startswith("sha256:") and len(in_feed.digest) == len("sha256:") + 64
    for changed in (
        raw.replace("Un livre", "Un livre!"),
        raw.replace("/opds/f/b.epub", "/opds/f/b2.epub"),
        raw.replace("espaces", "espace"),
        raw.replace(EPUB, CBZ),
    ):
        other = read_atom(atom_feed("urn:s", "Résultats", [changed])).entries[0]
        assert other.digest != in_feed.digest
    # the digest does not depend on the feed around the entry
    elsewhere = read_atom(atom_feed("urn:z", "Autre", [raw], links=[link("next", "/x", ATOM)]))
    assert elsewhere.entries[0].digest == in_feed.digest


@pytest.mark.req("EF-SRC-006")
def test_opds2_publication_digest_is_stable() -> None:
    pub = publication("pub-1", "Un livre", "/opds/f/pub-1.epub")
    a = read_json(opds2_feed("A", publications=[pub])).entries[0]
    b = read_json(opds2_feed("B", publications=[json.loads(json.dumps(pub))])).entries[0]
    assert a.digest == b.digest
    changed = json.loads(json.dumps(pub))
    changed["metadata"]["title"] = "Autre"
    assert read_json(opds2_feed("A", publications=[changed])).entries[0].digest != a.digest


# -- OpenSearch --------------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-006")
def test_opensearch_template() -> None:
    kavita = opensearch.parse_description(
        kavita_description("/opds/series?query={searchTerms}"), f"{BASE}search", safe_parse_xml
    )
    assert [u.template for u in kavita] == [
        "https://opds.example.org/opds/series?query={searchTerms}"
    ]
    komga = opensearch.parse_description(
        komga_description("/opds/v1.2/series?search={searchTerms}"), f"{BASE}search", safe_parse_xml
    )
    chosen = opensearch.pick_atom(komga)
    assert chosen is not None and "series?search=" in chosen.template  # not the HTML form
    url = opensearch.expand(chosen.template, "Un titre & plus #1")
    assert url == "https://opds.example.org/opds/v1.2/series?search=Un+titre+%26+plus+%231"
    # optional parameters are dropped, a required unknown one refuses
    assert (
        opensearch.expand("https://h.example/s?q={searchTerms}&p={startPage?}&l={language?}", "x")
        == "https://h.example/s?q=x"
    )
    assert opensearch.expand("https://h.example/s?q={searchTerms}&p={startPage?}", "x", page=3) == (
        "https://h.example/s?q=x&p=3"
    )
    with pytest.raises(FeedError):
        opensearch.expand("https://h.example/s?q={searchTerms}&z={mystery}", "x")
    with pytest.raises(FeedError):
        opensearch.expand("https://h.example/s?nothing", "x")  # no `{searchTerms}`
    # OPDS 2.0: RFC 6570 form-style query
    assert (
        opensearch.expand("https://h.example/s{?query}", "a b") == "https://h.example/s?query=a+b"
    )
    assert (
        opensearch.expand("https://h.example/s{?query,page}", "a") == "https://h.example/s?query=a"
    )


@pytest.mark.req("EF-SRC-006", "ENF-SEC-014")
def test_opensearch_terms_cannot_break_out_of_the_template() -> None:
    """Attack: a search term made to add a parameter, a host or a new line."""
    for terms in ("x&admin=1", "x#frag", "../../etc", "x\r\nHost: evil.example", "%00", "é" * 500):
        url = opensearch.expand("https://h.example/s?q={searchTerms}", terms)
        assert url.startswith("https://h.example/s?q=")
        assert "&admin" not in url and "#" not in url and "\r" not in url and "\n" not in url
        assert url.count("?") == 1 and "//" not in url.removeprefix("https://")


@pytest.mark.req("ENF-SEC-014")
def test_opensearch_description_that_is_hostile_is_refused() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]>'
        b'<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">&a;</OpenSearchDescription>'
    )
    with pytest.raises(FeedError):
        opensearch.parse_description(bomb, BASE, safe_parse_xml)
    with pytest.raises(FeedError):
        opensearch.parse_description(b"<html/>", BASE, safe_parse_xml)
