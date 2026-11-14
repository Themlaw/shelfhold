"""M3 L48 (EF-UX-004): the entry points of "Connecter un appareil" come from the Kavita address
and from nothing else: the address without its userinfo, the OPDS and kosync prefixes of Kavita,
never a key, a token or a query."""

import pytest

from shelfhold.export.domain.kavita_instance import device_entry_points

pytestmark = pytest.mark.req("EF-UX-004")


def test_entry_points_are_kavita_prefixes_without_a_key() -> None:
    found = device_entry_points("https://kavita.example/")
    assert found == {
        "kover": "https://kavita.example",
        "opds": "https://kavita.example/api/opds",
        "kosync": "https://kavita.example/api/koreader",
    }


def test_entry_points_strip_userinfo_query_and_fragment() -> None:
    found = device_entry_points("https://user:pw@kavita.example:8443/base/?apiKey=abc#frag")
    assert found == {
        "kover": "https://kavita.example:8443/base",
        "opds": "https://kavita.example:8443/base/api/opds",
        "kosync": "https://kavita.example:8443/base/api/koreader",
    }
    for value in found.values():
        assert not any(mark in value for mark in ("@", "?", "#", "abc", "pw", "user"))


@pytest.mark.parametrize("bad", ["", "javascript:alert(1)", "ftp://kavita.example", "kavita"])
def test_entry_points_refuse_anything_but_a_web_address(bad: str) -> None:
    assert device_entry_points(bad) is None
