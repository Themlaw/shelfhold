"""M4 L47: the small rules of the kosync service that need no application (EF-LEC-009)."""

import pytest

from shelfhold.reading.application.kosync import (
    InvalidProgressError,
    Pulled,
    UnknownCounter,
    _clean,
    _page,
    _xpointer,
)

pytestmark = pytest.mark.req("EF-LEC-009")


def test_the_counter_names_a_token_by_its_public_identifier_in_any_case() -> None:
    counter = UnknownCounter()
    counter.hit("AbCd1234")
    counter.hit("abcd1234")
    assert counter.of("ABCD1234") == 2 and counter.of("other") == 0
    assert counter.snapshot() == {"abcd1234": 2}


def test_the_counter_is_bounded_and_forgets_the_oldest_token() -> None:
    counter = UnknownCounter()
    for n in range(1100):
        counter.hit(f"t{n}")
    assert len(counter.snapshot()) == 1024 and counter.of("t0") == 0 and counter.of("t1099") == 1


def test_a_read_without_device_id_leaves_the_key_out() -> None:
    body = Pulled("d", "3", 0.5, "Libra", None, 10).body()
    assert "device_id" not in body and body["progress"] == "3"
    assert Pulled("d", "3", 0.5, "Libra", "x", 10).body()["device_id"] == "x"


@pytest.mark.parametrize("text, page", [("1", 1), (" 12 ", 12), ("1000000000", 10**9)])
def test_a_page_is_a_whole_number_from_one(text: str, page: int) -> None:
    assert _page(text) == page


@pytest.mark.parametrize("text", ["0", "-1", "1.0", "\uff11\uff12", "1000000001", "", "1_0", "0x1"])
def test_anything_else_is_not_a_page(text: str) -> None:
    with pytest.raises(InvalidProgressError):
        _page(text)


@pytest.mark.parametrize("text", ["", "a\x00b", "a\nb", "‮", "x" * 2049])
def test_an_xpointer_with_a_control_character_or_too_long_is_refused(text: str) -> None:
    with pytest.raises(InvalidProgressError):
        _xpointer(text)


def test_an_xpointer_is_kept_as_it_is() -> None:
    text = "/body/DocFragment[3]/body/div/p[12]/text().5"
    assert _xpointer(text) == text and _clean("Libra 2", limit=128) == "Libra 2"
