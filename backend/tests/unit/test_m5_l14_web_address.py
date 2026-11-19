"""M5 L14 (EF-LEC-008): the address of Kavita given to the browser never carries a secret."""

import pytest

from shelfhold.interfaces.http_api.reader import web_address

pytestmark = pytest.mark.req("EF-LEC-008")

USERINFO = "us" + "er:" + "pw" + "@"  # a placeholder, not a credential


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


@pytest.mark.parametrize("port", ["99999", "65536", "0", "-1", "abc"])
def test_a_port_out_of_range_gives_no_link_and_never_raises(port: str) -> None:
    assert web_address(f"https://kavita.example.test:{port}") is None


@pytest.mark.parametrize("port", ["99999", "65536", "0", "-1", "abc"])
def test_a_target_port_out_of_range_is_refused_on_creation_and_update(port: str) -> None:
    """`validate_target_url` is the one rule of `target add` and of the plan that updates a target
    (`_target_url`, `_service_url`): a port that is not 1..65535 is refused with the same error."""
    from shelfhold.headless.admin import InvalidConfigValue, _service_url, _target_url
    from shelfhold.headless.urls import validate_target_url

    url = f"https://kavita.example.test:{port}"
    with pytest.raises(ValueError, match="port"):
        validate_target_url(url)
    for refuse in (_target_url, lambda u: _service_url(u, "the Komga address")):
        with pytest.raises(InvalidConfigValue):
            refuse(url)
    assert validate_target_url("https://kavita.example.test:65535")
    assert validate_target_url("https://kavita.example.test:1")
