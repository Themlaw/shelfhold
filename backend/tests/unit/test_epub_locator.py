"""M5 L09 (EF-LEC-009, EF-LEC-014): the xpointer KOReader sends for an EPUB, read with bounds.

An xpointer is text from a device. It is **parsed** (never run, never turned into anything else),
bounded in length, in number of steps and in each name, and it gives one thing: the index of the
spine document it is in. Anything that does not fit the grammar is `None`: the caller keeps the
text as it came, as an opaque locator, and draws nothing from it.
"""

from __future__ import annotations

import pytest

from shelfhold.reading.domain.epub_locator import (
    END_SHARE,
    MAX_NAME,
    MAX_STEPS,
    MAX_XPOINTER,
    parse_xpointer,
    reaches_end,
    spine_index,
)

XP = "/body/DocFragment[3]/body/div/p[12]/text().5"


@pytest.mark.req("EF-LEC-009", "EF-LEC-014")
def test_epub_xpointer_maps_to_spine_index() -> None:
    """`DocFragment[n]` counts the documents of the spine from 1: the index counts from 0."""
    assert spine_index(XP) == 2
    assert spine_index("/body/DocFragment[1]/body/p") == 0
    assert spine_index("/body/DocFragment[1]") == 0
    assert spine_index("/body/DocFragment[12]/body/div[2]/p[7].14") == 11
    assert spine_index(XP, spine_len=3) == 2
    assert spine_index(XP, spine_len=10) == 2
    assert spine_index(XP, spine_len=2) is None  # a document the spine does not have
    assert spine_index(XP, spine_len=0) is None
    parsed = parse_xpointer(XP)
    assert parsed is not None and (parsed.fragment, parsed.offset) == (3, 5)


@pytest.mark.req("EF-LEC-009", "EF-LEC-014")
@pytest.mark.parametrize(
    "text",
    [
        "",
        "DocFragment[3]",
        "/body/p",
        "/body/DocFragment[0]/body/p",  # the first document is 1
        "/body/DocFragment[-1]/body/p",
        "/body/DocFragment[x]/body/p",
        "/body/DocFragment[3",
        "/body/DocFragment[1000000]/body/p",  # no book has a million documents
        "/body/DocFragment[٣]/body/p",  # not an ASCII digit
        "/body/DocFragment[3]/body/p[0x10]",
        "/body/DocFragment[3]/body/p/../../x",
        "/body/DocFragment[3]//p",
        "/body/DocFragment[3]/body/p\n",
        "/body/DocFragment[3]/body/p\x00",
        "/body/DocFragment[3]/body/<script>",
        "/body/DocFragment[3]/body/p'; DROP TABLE reading_position; --",
        "/body/DocFragment[3]/body/p/text().",
        "/body/DocFragment[3]/body/p/text().-5",
        "/body/DocFragment[3]/body/p.99999999999",
        '{"page": 99999}',
        "/body/DocFragment[3]/body/é",
        "/BODY/DocFragment[3]/body/p",  # the grammar is exact
    ],
)
def test_epub_xpointer_outside_the_grammar_gives_no_index(text: str) -> None:
    assert parse_xpointer(text) is None
    assert spine_index(text) is None
    assert reaches_end(text, 1.0, 3) is False


@pytest.mark.req("EF-LEC-009", "EF-LEC-014")
def test_epub_xpointer_is_walked_with_bounds() -> None:
    """Length, number of steps and length of a name each have a ceiling, to the unit."""
    steps = "/div" * (MAX_STEPS - 1)  # `body` below the fragment is a step too
    assert spine_index(f"/body/DocFragment[2]/body{steps}") == 1
    assert spine_index(f"/body/DocFragment[2]/body{steps}/p") is None  # one step more
    assert spine_index(f"/body/DocFragment[2]/body/{'a' * MAX_NAME}") == 1
    assert spine_index(f"/body/DocFragment[2]/body/{'a' * (MAX_NAME + 1)}") is None
    long = "/body/DocFragment[2]/body" + "/p" * (MAX_XPOINTER // 2)
    assert len(long) > MAX_XPOINTER and spine_index(long) is None
    assert spine_index("a" * (10 * MAX_XPOINTER)) is None  # refused by its length, never scanned


@pytest.mark.req("EF-LEC-009", "EF-LEC-014")
def test_epub_xpointer_parse_is_linear_on_hostile_input() -> None:
    """No pattern backtracks: a long run that almost fits answers at once."""
    hostile = "/body/DocFragment[3]/body" + "/p[" * 600
    assert parse_xpointer(hostile) is None
    assert parse_xpointer("/body/DocFragment[" + "9" * 2000 + "]") is None


@pytest.mark.req("EF-LEC-009", "EF-LEC-014")
def test_epub_the_end_is_the_last_document_of_the_spine_and_the_end_of_the_book() -> None:
    last = "/body/DocFragment[3]/body/div[40]/p[9]/text().412"
    assert reaches_end(last, 1.0, 3) is True
    assert reaches_end(last, END_SHARE, 3) is True
    assert reaches_end(last, END_SHARE - 0.001, 3) is False  # in the last document, not at its end
    assert reaches_end("/body/DocFragment[2]/body/p", 1.0, 3) is False  # not the last document
    assert reaches_end(last, 1.0, 5) is False  # the spine is longer than that
    assert reaches_end(last, 1.0, None) is False  # no spine known: nothing is read
    assert reaches_end(last, 1.0, 0) is False
    assert reaches_end(last, float("nan"), 3) is False
    assert reaches_end(last, 1.5, 3) is False
