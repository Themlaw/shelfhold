"""M2 L23 (EF-SRC-007): the official catalogue, read as CSV or RDF. A catalogue is data from the
network: whatever it holds, it is read as text, never executed, never resolved."""

from __future__ import annotations

import pytest

from shelfhold.plugin_host.adapters.native.gutenberg.catalog import (
    CatalogError,
    parse_catalog,
    parse_csv,
    parse_rdf,
)
from tests.gutenberg_env import CSV, TRANSLATIONS_RDF

pytestmark = pytest.mark.req("EF-SRC-007")


def test_csv_gives_books_with_authors_and_death_years() -> None:
    books = {b.pg_id: b for b in parse_csv(CSV.encode())}
    assert set(books) == {1001, 3001, 4001, 6001, 8001}  # the audio row is not a book
    doe = books[1001]
    assert doe.title == "A Study in Test" and doe.languages == ("en",)
    assert [(a.name, a.death_year) for a in doe.authors] == [("Doe, Jane", 1920)]
    assert books[8001].authors[0].death_year is None


def test_csv_with_a_byte_order_mark_and_crlf_and_extra_columns() -> None:
    raw = "﻿Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves,Extra\r\n"
    raw += '1,Text,2001-01-01,T,en;fr,"A, B, 1900-1950",,,,x\r\n'
    (book,) = parse_csv(raw.encode())
    assert book.languages == ("en", "fr") and book.authors[0].death_year == 1950


@pytest.mark.parametrize(
    "row",
    [
        "x,Text,2001,Bad id,en,,,,",
        "-5,Text,2001,Negative,en,,,,",
        "99999999999999999999,Text,2001,Huge,en,,,,",
        "7,Text,2001,,en,,,,",  # no title
    ],
)
def test_csv_rows_that_are_not_books_are_skipped_not_fatal(row: str) -> None:
    raw = "Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n" + row + "\n"
    assert parse_csv(raw.encode()) == []


def test_csv_without_the_expected_header_is_refused() -> None:
    with pytest.raises(CatalogError):
        parse_csv(b"a,b,c\n1,2,3\n")


def test_rdf_gives_translators_and_publisher() -> None:
    books = {b.pg_id: b for b in parse_rdf(TRANSLATIONS_RDF)}
    assert set(books) == {7001, 7002}
    assert books[7001].translators == ("Ames, Alice",) and books[7002].translators == ("Bell, Bruno",)
    assert books[7001].publisher == "Project Gutenberg" and books[7001].authors[0].death_year == 1870


@pytest.mark.parametrize(
    "payload",
    [
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><r>&b;</r>',
        b'<!DOCTYPE r SYSTEM "file:///etc/passwd"><r/>',
        b'<?xml version="1.0"?><!doctype r [<!ENTITY x SYSTEM "http://127.0.0.1/">]><r>&x;</r>',
    ],
)
def test_rdf_with_a_doctype_or_entities_is_refused_without_expansion(payload: bytes) -> None:
    """Attack: an entity bomb or an external entity in the catalogue."""
    with pytest.raises(CatalogError):
        parse_rdf(payload)
    with pytest.raises(CatalogError):
        parse_catalog(payload)


def test_the_format_is_read_from_the_bytes() -> None:
    assert len(parse_catalog(CSV.encode())) == 5
    assert len(parse_catalog(b"  \n" + TRANSLATIONS_RDF)) == 2
    with pytest.raises(CatalogError):
        parse_catalog(b"")


def test_a_catalogue_with_too_many_books_is_refused() -> None:
    head = "Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
    body = "".join(f"{i},Text,2001,T{i},en,,,,\n" for i in range(1, 30))
    with pytest.raises(CatalogError):
        parse_csv((head + body).encode(), limit=10)
