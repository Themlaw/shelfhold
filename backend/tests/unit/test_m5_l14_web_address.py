"""M5 L14 (EF-LEC-008): the address of Kavita given to the browser never carries a secret."""

import pytest

from shelfhold.interfaces.http_api.reader import web_address

pytestmark = pytest.mark.req("EF-LEC-008")

USERINFO = "u" + "ser:" + "pw" + "@"  # a placeholder, not a credential


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://kavita.example.test:5000", "http://kavita.example.test:5000"),
        (
            "https://kavita.example.test/library?x=1#top",
            "https://kavita.example.test/library?x=1#top",
        ),
        (f"https://{USERINFO}kavita.example.test:8443/a", "https://kavita.example.test:8443/a"),
        (f"http://{USERINFO[:-1]}@[::1]:5000/", "http://[::1]:5000/"),
        ("javascript:alert(1)", None),
        ("data:text/html,x", None),
        ("ftp://kavita.example.test", None),
        ("//kavita.example.test", None),
        ("http://", None),
        ("", None),
        (None, None),
    ],
)
def test_the_address_has_no_userinfo_and_only_http(given: str | None, expected: str | None) -> None:
    got = web_address(given)
    assert got == expected
    assert got is None or "@" not in got.split("/", 3)[2]
