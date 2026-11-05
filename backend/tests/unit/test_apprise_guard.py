"""ENF-SEC-016, EF-INT-006: only allowed schemes, never an undeclared internal destination."""

from __future__ import annotations

import asyncio
from importlib.metadata import version

import pytest

from shelfhold.infrastructure.fetcher import LanTargets
from shelfhold.integrations.apprise import (
    ALLOWED_SCHEMES,
    ChannelUrlRefused,
    validate_channel_url,
)
from tests.fakes_fetcher import FakeResolver

PUBLIC = "93.184.216.34"
RESOLVER = FakeResolver(
    {
        "ntfy.example.org": [PUBLIC],
        "internal.example.org": ["10.0.0.7"],
        "rebind.example.org": ["169.254.169.254"],
        "mixed.example.org": [PUBLIC, "192.168.1.9"],
        "nas.lan": ["192.168.1.20"],
        "localhost": ["127.0.0.1"],
    }
)


def check(url: str, lan: LanTargets | None = None) -> None:
    asyncio.run(validate_channel_url(url, lan=lan or LanTargets.empty(), resolver=RESOLVER))


@pytest.mark.req("ENF-SEC-016", "EF-INT-006")
@pytest.mark.parametrize(
    "url",
    [
        "ntfy://93.184.216.34/topic",
        "ntfys://93.184.216.34/topic",
        "NTFYS://93.184.216.34/topic",
        "gotifys://93.184.216.34:8443/token",
        "discord://12345678/abcdefgh-token",
        "slack://T0000000/B0000000/XXXXXXXXXXXXXXXXXXXXXXXX",
        "tgram://123456:bot-token/42",
        "matrixs://user:pw@93.184.216.34/room",
    ],
)
def test_apprise_schemes_allowlisted_accepts(url: str) -> None:
    check(url)


@pytest.mark.req("ENF-SEC-016", "EF-INT-006")
@pytest.mark.parametrize(
    "url",
    [
        "json://ntfy.example.org/hook",  # generic webhooks reach any host
        "jsons://ntfy.example.org/hook",
        "form://ntfy.example.org/hook",
        "xml://ntfy.example.org/hook",
        "file:///etc/passwd",
        "dbus://",
        "gnome://",
        "windows://",
        "macosx://",
        "apprise://ntfy.example.org/key",
        "apprises://ntfy.example.org/key",
        "cfg://ntfy.example.org/x",
        "http://ntfy.example.org/",
        "https://ntfy.example.org/",
        "ftp://ntfy.example.org/",
        "unknownscheme://ntfy.example.org/",
        "ntfy.example.org/topic",
        "",
        "ntfy:/ntfy.example.org",
        " ntfy://ntfy.example.org/topic\nxml://x",
        "ntfy://ntfy.example.org/topic xml://other.example.org/",
    ],
)
def test_apprise_schemes_allowlisted_refuses(url: str) -> None:
    with pytest.raises(ChannelUrlRefused):
        check(url)


@pytest.mark.req("ENF-SEC-016")
def test_apprise_allowlist_is_small_and_has_no_local_or_generic_scheme() -> None:
    assert {"ntfy", "ntfys", "gotify", "gotifys", "discord", "slack", "tgram"} <= ALLOWED_SCHEMES
    for forbidden in ("json", "jsons", "form", "forms", "xml", "xmls", "file", "dbus", "apprise"):
        assert forbidden not in ALLOWED_SCHEMES
    assert len(ALLOWED_SCHEMES) <= 20


@pytest.mark.req("ENF-SEC-016", "EF-INT-006")
@pytest.mark.parametrize(
    "url",
    [
        "ntfy://127.0.0.1/topic",
        "ntfy://localhost/topic",
        "gotify://10.1.2.3/token",
        "gotify://[::1]/token",
        "ntfy://169.254.169.254/latest",
        "ntfy://internal.example.org/topic",  # a public name that points inside
        "ntfy://rebind.example.org/topic",
        "ntfy://mixed.example.org/topic",
        "ntfy://nas.lan/topic",  # undeclared
        "ntfy://ntfy.example.org/topic",  # a public name: Apprise would resolve it again itself
        "ntfys://93.184.216.34/topic?verify=no",  # the certificate is always verified
        "gotifys://93.184.216.34/token?verify=no",
        "ntfy://0.0.0.0/topic",
        "ntfy://2130706433/topic",  # decimal 127.0.0.1
    ],
)
def test_internal_destinations_refused_unless_declared(url: str) -> None:
    with pytest.raises(ChannelUrlRefused):
        check(url)


@pytest.mark.req("ENF-SEC-016", "EF-INT-006")
def test_declared_lan_target_is_allowed_but_never_the_metadata_service() -> None:
    lan = asyncio.run(LanTargets.declare(["nas.lan:80", "192.168.1.30:8080"], RESOLVER))
    check("ntfy://nas.lan/topic", lan)
    check("gotify://192.168.1.30:8080/token", lan)
    with pytest.raises(ChannelUrlRefused):  # declared host, other port
        check("ntfy://nas.lan:8081/topic", lan)
    with pytest.raises(ChannelUrlRefused):
        check("ntfy://169.254.169.254/topic", lan)


@pytest.mark.req("ENF-SEC-016")
def test_redirect_parameter_cannot_reenable_redirects() -> None:
    checked = asyncio.run(
        validate_channel_url(
            "ntfy://93.184.216.34/topic?redirect=yes", lan=LanTargets.empty(), resolver=RESOLVER
        )
    )
    assert checked.follows_redirects is False


@pytest.mark.req("ENF-SEC-016")
def test_apprise_floor_is_the_v2_api() -> None:
    import apprise

    major, minor, *_ = (int(p) for p in version("apprise").split(".")[:2])
    assert (2, 1) <= (major, minor) < (3, 0)
    assert hasattr(apprise, "AppriseResult")  # the v2 result per service (failure matrix)


@pytest.mark.req("ENF-SEC-016")
def test_url_secrets_are_registered_and_repr_hides_the_url() -> None:
    from shelfhold.infrastructure import logging as logs
    from shelfhold.integrations.apprise import ValidatedChannel

    logs.forget_secret_values()
    checked = asyncio.run(
        validate_channel_url(
            "ntfy://user:pa55word-secret@93.184.216.34/seg-secret-one/seg-secret-two"
            "?tags=qv-secret-value",
            lan=LanTargets.empty(),
            resolver=RESOLVER,
        )
    )
    assert "pa55word-secret" not in repr(checked)
    assert isinstance(checked, ValidatedChannel)
    for secret in ("pa55word-secret", "seg-secret-one", "seg-secret-two", "qv-secret-value"):
        assert secret in logs._known_values
    logs.forget_secret_values()
