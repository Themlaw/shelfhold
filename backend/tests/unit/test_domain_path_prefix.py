"""M2 L23 (EF-SRC-003, ENF-LEG-003): a DomainRule can be limited to a path, so that a host the
admin opened for one official feed is not opened for the rest of its site."""

from __future__ import annotations

import pytest

from shelfhold.infrastructure.fetcher import DomainNotAllowed
from shelfhold.infrastructure.fetcher.guard import DomainAllowlist
from shelfhold.sdk.models import DomainRule

pytestmark = pytest.mark.req("EF-SRC-003", "ENF-LEG-003")

FEED = "/cache/epub/feeds/"


def rules(*extra: DomainRule) -> list[DomainRule]:
    return [DomainRule(pattern="www.example.org", purpose="api", path_prefix=FEED), *extra]


def test_a_rule_without_a_prefix_is_as_before() -> None:
    assert DomainRule(pattern="a.example.org", purpose="api").path_prefix is None
    allow = DomainAllowlist("p", [DomainRule(pattern="a.example.org", purpose="api")])
    allow.check("a.example.org", "a.example.org/anything", path="/anything")


def test_the_prefix_limits_the_host_to_that_path() -> None:
    allow = DomainAllowlist("p", rules())
    allow.check("www.example.org", "www.example.org/x", path=FEED + "pg_catalog.csv")
    for path in ("/", "/ebooks/1", "/cache/epub/feeds", "/cache/epub/feedsX/a", "/x" + FEED):
        with pytest.raises(DomainNotAllowed):
            allow.check("www.example.org", "www.example.org" + path, path=path)


@pytest.mark.parametrize(
    "path",
    [
        FEED + "../../ebooks/1",
        FEED + "%2e%2e/ebooks/1",
        FEED + "%2E%2E/ebooks/1",
        "/cache/epub/feeds%2f..%2febooks",
        FEED + "..%2febooks",
        FEED + "a/./../../x",
        FEED + "a\\..\\x",
        FEED + "%00",
    ],
)
def test_a_path_that_climbs_out_of_the_prefix_is_refused(path: str) -> None:
    allow = DomainAllowlist("p", rules())
    with pytest.raises(DomainNotAllowed):
        allow.check("www.example.org", "www.example.org/x", path=path)


def test_another_rule_for_the_same_host_without_a_prefix_opens_it_whole() -> None:
    allow = DomainAllowlist("p", rules(DomainRule(pattern="www.example.org", purpose="other")))
    allow.check("www.example.org", "www.example.org/ebooks/1", path="/ebooks/1")


def test_the_override_of_a_setting_keeps_the_prefix() -> None:
    rule = DomainRule(
        pattern="catalog.invalid", purpose="api", base_url_setting="catalog_url", path_prefix=FEED
    )
    allow = DomainAllowlist("p", [rule], {"catalog_url": "https://www.example.org" + FEED + "f.csv"})
    allow.check("www.example.org", "r", path=FEED + "f.csv")
    with pytest.raises(DomainNotAllowed):
        allow.check("www.example.org", "r", path="/ebooks/1")
    with pytest.raises(DomainNotAllowed):
        allow.check("catalog.invalid", "r", path=FEED)  # the declared host is replaced


def test_a_prefix_that_is_not_an_absolute_path_is_not_a_rule() -> None:
    for bad in ("cache/", "", "/a/../b/", "/a//b/", "https://x/"):
        with pytest.raises(ValueError):
            DomainRule(pattern="a.example.org", purpose="api", path_prefix=bad)
