"""M4 L18a (EF-BIB-016): the reading-list file is untrusted XML, read bounded, with no entity.

The parser keeps the order of the books, refuses every DTD (so no external entity, no parameter
entity, no expansion), and stops at a size, a depth, a number of elements or of books that a
reading list never needs.
"""

import pytest

from shelfhold.catalog.adapters.cbl import MAX_BOOKS, MAX_CBL_BYTES, CblError, parse_cbl

pytestmark = pytest.mark.req("EF-BIB-016", "ENF-SEC-014")

HEAD = b'<?xml version="1.0" encoding="utf-8"?>'


def _list(books: list[tuple[str, str]], *, name: str = "Order") -> bytes:
    rows = b"".join(
        f'<Book Series="{series}" Number="{number}" Volume="2020" Year="2021"/>'.encode()
        for series, number in books
    )
    return (
        HEAD
        + b"<ReadingList><Name>"
        + name.encode()
        + b"</Name><Books>"
        + rows
        + b"</Books></ReadingList>"
    )


def test_the_books_come_in_the_order_of_the_file() -> None:
    parsed = parse_cbl(_list([("Zeta", "3"), ("Alpha", "1"), ("Zeta", "2")], name="Mine"))
    assert parsed.name == "Mine"
    assert [(b.series, b.number) for b in parsed.books] == [
        ("Zeta", "3"),
        ("Alpha", "1"),
        ("Zeta", "2"),
    ]
    assert (parsed.books[0].volume, parsed.books[0].year) == ("2020", "2021")


@pytest.mark.req("EF-BIB-016", "ENF-SEC-014")
def test_cbl_parser_refuses_external_entities() -> None:
    secret = b"root:x:0:0"
    file_entity = (
        HEAD + b'<!DOCTYPE ReadingList [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
        b"<ReadingList><Name>&leak;</Name><Books/></ReadingList>"
    )
    network_entity = (
        HEAD + b'<!DOCTYPE ReadingList [<!ENTITY leak SYSTEM "http://127.0.0.1:9/x">]>'
        b"<ReadingList><Name>&leak;</Name><Books/></ReadingList>"
    )
    parameter_entity = (
        HEAD + b'<!DOCTYPE ReadingList [<!ENTITY % p SYSTEM "http://127.0.0.1:9/x.dtd"> %p;]>'
        b"<ReadingList><Name>x</Name><Books/></ReadingList>"
    )
    external_dtd = (
        HEAD + b'<!DOCTYPE ReadingList SYSTEM "http://127.0.0.1:9/x.dtd">'
        b"<ReadingList><Name>x</Name><Books/></ReadingList>"
    )
    for document in (file_entity, network_entity, parameter_entity, external_dtd):
        with pytest.raises(CblError) as refused:
            parse_cbl(document)
        assert refused.value.reason in {"dtd", "entity", "external"}
        assert secret.decode() not in str(refused.value)


def test_an_expanding_entity_is_refused_before_it_expands() -> None:
    laughs = (
        HEAD
        + b'<!DOCTYPE ReadingList [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">'
        b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;">]>'
        b"<ReadingList><Name>&c;</Name><Books/></ReadingList>"
    )
    with pytest.raises(CblError) as refused:
        parse_cbl(laughs)
    assert refused.value.reason in {"dtd", "entity"}


def test_a_document_over_the_size_cap_is_refused_unread() -> None:
    with pytest.raises(CblError) as refused:
        parse_cbl(b"<ReadingList>" + b" " * MAX_CBL_BYTES + b"</ReadingList>")
    assert refused.value.reason == "too_large"


def test_a_deep_document_is_refused() -> None:
    nested = b"<a>" * 40 + b"</a>" * 40
    with pytest.raises(CblError) as refused:
        parse_cbl(HEAD + b"<ReadingList><Name>x</Name><Books>" + nested + b"</Books></ReadingList>")
    assert refused.value.reason == "too_deep"


def test_more_books_than_the_cap_are_refused_not_cut() -> None:
    with pytest.raises(CblError) as refused:
        parse_cbl(_list([("Tale", str(i)) for i in range(MAX_BOOKS + 1)]))
    assert refused.value.reason == "too_many_books"


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (b"", "malformed"),
        (b"<ReadingList><Name>x</Name>", "malformed"),
        (HEAD + b"<Playlist><Name>x</Name></Playlist>", "not_a_reading_list"),
        (HEAD + b"<ReadingList><Name>x</Name></ReadingList>", "not_a_reading_list"),
        (
            HEAD + b"<ReadingList><Name>x</Name><Books><Book Number='1'/></Books></ReadingList>",
            "malformed",
        ),
    ],
)
def test_what_is_not_a_reading_list_is_refused_with_a_word(document: bytes, reason: str) -> None:
    with pytest.raises(CblError) as refused:
        parse_cbl(document)
    assert refused.value.reason == reason


def test_a_text_that_is_too_long_is_refused() -> None:
    with pytest.raises(CblError) as refused:
        parse_cbl(_list([("T" * 5000, "1")]))
    assert refused.value.reason == "too_long"


def test_only_bytes_are_read() -> None:
    with pytest.raises(TypeError):
        parse_cbl("<ReadingList/>")  # type: ignore[arg-type]
