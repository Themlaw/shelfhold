"""M3 L48 (EF-UX-004): the entry points of "Connect a device" come from the Kavita address
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
    # the configured sub-path (`/base`) stays; userinfo, query and fragment never do
    found = device_entry_points("https://user:pw@kavita.example:8443/base/?apiKey=abc#frag")
    assert found == {
        "kover": "https://kavita.example:8443/base",
        "opds": "https://kavita.example:8443/base/api/opds",
        "kosync": "https://kavita.example:8443/base/api/koreader",
    }
    for value in found.values():
        assert not any(mark in value for mark in ("@", "?", "#", "abc", "pw", "user"))


def test_qr_keeps_configured_kavita_subpath_only() -> None:
    """A reverse proxy prefix is kept; everything from `/api/` on is cut, so a reader's key
    (`/kavita/api/opds/<key>`) never reaches the QR."""
    expected = {
        "kover": "https://kavita.example/kavita",
        "opds": "https://kavita.example/kavita/api/opds",
        "kosync": "https://kavita.example/kavita/api/koreader",
    }
    assert device_entry_points("https://kavita.example/kavita") == expected
    assert device_entry_points("https://kavita.example/kavita/") == expected
    assert device_entry_points("https://kavita.example/kavita/api/opds/SECRETKEY") == expected
    assert device_entry_points("https://kavita.example/kavita/api") == expected
    assert device_entry_points("https://kavita.example/kavita/API/opds/SECRETKEY") == expected
    found = device_entry_points("https://kavita.example/api/opds/SECRETKEY")
    assert found is not None and found["kover"] == "https://kavita.example"
    deep = device_entry_points("https://kavita.example/a/b/api/opds/SECRETKEY")
    assert deep is not None and deep["kover"] == "https://kavita.example/a/b"
    for value in (*expected.values(), *(found or {}).values(), *(deep or {}).values()):
        assert "SECRETKEY" not in value


def test_a_path_that_only_looks_like_the_api_prefix_is_kept() -> None:
    found = device_entry_points("https://kavita.example/apiary/kavita")
    assert found is not None and found["kover"] == "https://kavita.example/apiary/kavita"


def test_a_path_parameter_never_carries_a_key_into_the_sub_path() -> None:
    found = device_entry_points("https://kavita.example/kavita;apiKey=SECRETKEY/x")
    assert found is not None and found["kover"] == "https://kavita.example/kavita/x"


def test_entry_points_with_an_ipv6_host_and_a_subpath() -> None:
    found = device_entry_points("http://[::1]:5000/kavita/api/opds/KEY?x=1")
    assert found == {
        "kover": "http://[::1]:5000/kavita",
        "opds": "http://[::1]:5000/kavita/api/opds",
        "kosync": "http://[::1]:5000/kavita/api/koreader",
    }


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "javascript:alert(1)",
        "ftp://kavita.example",
        "kavita",
        "https://kavita.example:0x1",
        "https://kavita.example:99999",
    ],
)
def test_entry_points_refuse_anything_but_a_web_address(bad: str) -> None:
    assert device_entry_points(bad) is None
