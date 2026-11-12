"""Owner decision 2026-11-12: a frozen allow-list of the official Gutenberg mirrors (it holds
`aleph.gutenberg.org`) may be the mirror of the source. The website itself stays refused, and so
does every other host under the project's names. Nothing here reaches a network."""

from __future__ import annotations

import pytest

from shelfhold.acquisition.public import AcquireRefused
from shelfhold.infrastructure.fetcher import DomainNotAllowed
from shelfhold.plugin_host.adapters.native.gutenberg import mirror
from shelfhold.sdk.source import NotConfigured
from tests.gutenberg_env import (
    CATALOG_URL,
    CLEAN,
    OFFICIAL_MIRROR_HOST,
    epub_path,
    gutenberg_bench,
)

OFFICIAL = f"https://{OFFICIAL_MIRROR_HOST}/gutenberg"


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
def test_the_list_is_frozen_and_holds_aleph_but_never_the_website() -> None:
    hosts = mirror.OFFICIAL_MIRRORS
    assert isinstance(hosts, frozenset)
    assert "aleph.gutenberg.org" in hosts
    assert not hosts & {"www.gutenberg.org", "gutenberg.org", "gutenberg.net", "www.gutenberg.net"}
    assert all(h == h.lower() and "/" not in h and not h.startswith(".") for h in hosts)


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
@pytest.mark.parametrize(
    "url",
    [
        "https://aleph.gutenberg.org/gutenberg",
        "https://ALEPH.Gutenberg.org./gutenberg",
        "https://ａleph.gutenberg.org/gutenberg",  # a full-width letter: the same host
        "http://aleph.gutenberg.org/",
    ],
)
def test_an_official_mirror_is_accepted_as_the_mirror(url: str) -> None:
    assert mirror.setting_url({"mirror_url": url}, "mirror_url") == url.strip()


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
@pytest.mark.parametrize(
    "url",
    [
        "https://www.gutenberg.org/",
        "https://gutenberg.org/cache/epub",
        "https://www.gutenberg.net/",
        "https://gutenberg.net/",
        "https://evil.aleph.gutenberg.org/",  # under an official name is not the official name
        "https://xaleph.gutenberg.org/",
        "https://aleph2.gutenberg.org/",
        "https://dev.gutenberg.org/",
        "https://aleph.gutenberg.org@www.gutenberg.org/",  # a credential that names the site
        "https://user:pw@aleph.gutenberg.org/",
        "https://aleph.gutenberg.org/?next=www.gutenberg.org",
        "https://aleph.gutenberg.org/#www.gutenberg.org",
        "ftp://aleph.gutenberg.org/",
    ],
)
def test_the_website_and_lookalikes_stay_refused_as_the_mirror(url: str) -> None:
    with pytest.raises(NotConfigured):
        mirror.setting_url({"mirror_url": url}, "mirror_url")


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
def test_an_official_mirror_is_not_a_catalogue_host() -> None:
    """The allow-list is for files: the catalogue is read on the feeds of the site or elsewhere."""
    with pytest.raises(NotConfigured):
        mirror.setting_url(
            {"catalog_url": f"https://{OFFICIAL_MIRROR_HOST}/cache/epub/feeds/pg_catalog.csv"},
            "catalog_url",
        )


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
def test_a_book_comes_from_an_official_mirror_and_nothing_reaches_the_website() -> None:
    settings = {"catalog_url": CATALOG_URL, "mirror_url": OFFICIAL}
    with gutenberg_bench(settings=settings, files={epub_path(1001): CLEAN}) as b:
        b.acquire(1001)
        assert b.wire.to(OFFICIAL_MIRROR_HOST)
        assert not b.wire.site


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003", "EF-SRC-003")
@pytest.mark.parametrize(
    "url",
    [
        "https://www.gutenberg.org/ebooks/1001",
        "https://gutenberg.org/",
        "https://other.gutenberg.org/gutenberg/cache/epub/1001/pg1001.epub",
        "https://aleph.gutenberg.org.example.net/gutenberg/cache/epub/1001/pg1001.epub",
    ],
)
def test_with_an_official_mirror_set_every_other_host_stays_refused_by_the_domain_rules(
    url: str,
) -> None:
    settings = {"catalog_url": CATALOG_URL, "mirror_url": OFFICIAL}
    with gutenberg_bench(settings=settings) as b:
        fetcher = b.fetchers.for_record(b.registry.get("gutenberg"), b.settings)
        with pytest.raises(DomainNotAllowed):
            b.run(fetcher.get(url))
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003", "EF-SRC-002")
def test_an_official_mirror_that_redirects_to_the_website_is_refused_at_the_hop() -> None:
    settings = {"catalog_url": CATALOG_URL, "mirror_url": OFFICIAL}
    target = "https://www.gutenberg.org/ebooks/1001.epub.noimages"
    with gutenberg_bench(settings=settings, wire={"redirect_files_to": target}) as b:
        _prepared, outcome = b.acquire(1001)
        assert outcome.state == "abandoned" and outcome.reason == "source.domain_not_allowed"
        assert not b.wire.site_pages


@pytest.mark.req("EF-SRC-007")
def test_a_site_mirror_is_still_not_configured_and_sends_nothing() -> None:
    settings = {"catalog_url": CATALOG_URL, "mirror_url": "https://www.gutenberg.org/gutenberg"}
    with gutenberg_bench(settings=settings) as b:
        with pytest.raises(AcquireRefused) as raised:
            b.acquire(1001)
        assert raised.value.code == "source.not_configured"
        assert not b.wire.recorder.requests
