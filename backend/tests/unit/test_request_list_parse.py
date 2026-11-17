"""M4 L31 (EF-USR-020): reading a list of wanted books written as plain text, as untrusted input."""

import pytest

from shelfhold.acquisition.domain.request_list import (
    MAX_LINE_CHARS,
    MAX_LINES,
    MAX_LIST_BYTES,
    ListRefusedError,
    annotated_edition_asked,
    parse_list,
)

pytestmark = pytest.mark.req("EF-USR-020")
ISBN13 = "9780306406157"  # the well-known sample number, no real book


def test_one_line_is_one_title_with_an_isbn_if_there_is_one() -> None:
    lines = parse_list(
        f"# comment\n\nA Title\nB Title ; 978-0-306-40615-7\nC Title ({ISBN13})\n"
        f"D Title\t0-306-40615-2\n{ISBN13}\n"
    )
    assert [(ln.position, ln.text, ln.isbn) for ln in lines] == [
        (0, "A Title", None), (1, "B Title", ISBN13), (2, "C Title", ISBN13),
        (3, "D Title", ISBN13), (4, "", ISBN13),
    ]  # fmt: skip


def test_digits_that_are_not_an_isbn_stay_part_of_the_title() -> None:
    (line,) = parse_list("1984 and 2001, a 1234567890 odyssey")
    assert line.isbn is None and "1984" in line.text and "1234567890" in line.text


def test_a_list_over_a_bound_is_refused_whole_never_cut() -> None:
    cases = {
        "too_large": "a\n" * (MAX_LIST_BYTES // 2 + 1),
        "too_many_lines": "\n".join(f"t{i}" for i in range(MAX_LINES + 1)),
        "line_too_long": "t" * (MAX_LINE_CHARS + 1),
        "not_text": "title‮gnp.exe",  # a bidi override is never part of a title
        "empty": "# nothing\n  \n",
    }
    for reason, text in cases.items():
        with pytest.raises(ListRefusedError) as refused:
            parse_list(text)
        assert refused.value.reason == reason
    assert len(parse_list("\n".join(f"t{i}" for i in range(MAX_LINES)))) == MAX_LINES


def test_bytes_that_are_not_utf8_are_refused_without_an_echo() -> None:
    with pytest.raises(ListRefusedError) as refused:
        parse_list(b"\xff\xfe title")
    assert refused.value.reason == "not_text" and "title" not in str(refused.value)
    assert parse_list(b"\xef\xbb\xbfA Title")[0].text == "A Title"  # a byte order mark is dropped


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("Tale (annotated edition)", True), ("Édition critique de Tale", True),
        ("Tale, with notes", True), ("A critical edition of Tale", True),
        ("Tale commentée", True), ("Plain Tale", False), ("Annotation of a Tale", True),
        ("The Notebook", False),
    ],
)  # fmt: skip
def test_what_asks_for_an_annotated_edition(text: str, asked: bool) -> None:
    assert annotated_edition_asked(text) is asked
