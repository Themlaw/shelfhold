"""M2 L24 (EF-SRC-011, ENF-LEG-003, ENF-LEG-008, EF-SRC-013): the manifest of the Standard Ebooks
source, the way it reads a pasted address and a book path. No network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from shelfhold.infrastructure.xml import safe_parse_xml
from shelfhold.plugin_host.adapters.native.standardebooks import (
    StandardEbooksSource,
    build_manifest,
)
from shelfhold.plugin_host.adapters.native.standardebooks.source import parse_slug
from shelfhold.plugin_host.registry import compile_url_pattern
from shelfhold.plugin_host.source_fetcher import authenticated
from shelfhold.sdk.models import AuthBasic
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import AuthRequired, ContentRemoved
from shelfhold.sdk.testing.fakes import FakeFetcher

ROOT = Path(__file__).resolve().parents[3]
SITE = "https://standardebooks.org"


@pytest.mark.req("EF-SRC-011", "ENF-LEG-003")
def test_the_manifest_file_is_the_one_the_source_declares() -> None:
    path = ROOT / "sources/standardebooks/manifest.json"
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(
        build_manifest().model_dump_json()
    )


@pytest.mark.req("EF-SRC-011", "ENF-LEG-003", "ENF-LEG-008", "EF-BIB-051")
def test_the_manifest_declares_its_two_places_a_slow_rate_us_rights_and_untrusted_files() -> None:
    m = build_manifest()
    assert m.terms_url and m.respects_robots and m.attribution and "United States" in m.attribution
    assert m.content_trust == "untrusted" and m.rights_basis == "us_public_domain"
    assert m.rights_jurisdiction == "US"
    assert m.rate_limit.concurrency == 1 and m.rate_limit.requests_per_second <= 0.5
    rules = {(r.pattern, r.path_prefix) for r in m.permissions.domains}
    assert rules == {("standardebooks.org", "/ebooks/"), ("standardebooks.org", "/feeds/")}
    assert all(r.base_url_setting is None for r in m.permissions.domains)  # no address to swap
    assert [type(a) for a in m.auth] == [AuthBasic]


@pytest.mark.req("EF-SRC-011", "EF-SRC-004")
def test_the_url_patterns_take_a_book_and_nothing_longer_or_shorter() -> None:
    patterns = [compile_url_pattern(p) for p in build_manifest().url_patterns]
    for url in (f"{SITE}/ebooks/jane-synth/le-test", f"{SITE}/ebooks/homer/odyssey/some-person"):
        assert any(p.matches(url) for p in patterns)
    for url in (
        f"{SITE}/ebooks/jane-synth",
        f"{SITE}/ebooks/jane-synth/le-test/downloads/x.epub",
        f"{SITE}/feeds/opds",
        "https://standardebooks.org.evil.example/ebooks/a/b",
    ):
        assert not any(p.matches(url) for p in patterns)


@pytest.mark.req("EF-SRC-011", "ENF-SEC-014")
@pytest.mark.parametrize("slug", ["jane-synth/le-test", "homer/the-odyssey/some-person", "a/b"])
def test_a_book_path_is_author_title_and_maybe_a_person(slug: str) -> None:
    assert parse_slug(slug) == slug


@pytest.mark.req("EF-SRC-011", "ENF-SEC-014")
@pytest.mark.parametrize(
    "slug",
    [
        "",
        "a",
        "A/b",
        "a/B",
        "a/b/c/d",
        "a//b",
        "a/b/",
        "/a/b",
        "a/../b",
        "a/%2e%2e",
        "a/b?x",
        "a/b#x",
        "a/b\n",
        "a b/c",
        "-a/b",
        "a-/b",
        "a/b/downloads",
        "a/b/text",
        "é/b",
    ],
)
def test_anything_else_is_not_a_book_path(slug: str) -> None:
    with pytest.raises(ContentRemoved):
        parse_slug(slug)


@pytest.mark.req("EF-SRC-011", "EF-SRC-004")
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{SITE}/ebooks/jane-synth/le-test", "jane-synth/le-test"),
        (f"{SITE}/ebooks/jane-synth/le-test?x=1#y", "jane-synth/le-test"),
        ("https://STANDARDEBOOKS.ORG/ebooks/jane-synth/le-test", "jane-synth/le-test"),
        (f"{SITE}/ebooks/jane-synth/le-test/downloads", None),
        (f"{SITE}/ebooks/jane-synth", None),
        (f"{SITE}/feeds/opds/all", None),
        (f"{SITE}/ebooks/jane-synth/le-test/text/ch-1", None),
        ("https://www.standardebooks.org/ebooks/jane-synth/le-test", None),
        ("http://standardebooks.org/ebooks/jane-synth/le-test", None),
        (f"{SITE}:8443/ebooks/jane-synth/le-test", None),
    ],
)
def test_a_pasted_address_names_a_book_or_nothing(url: str, expected: str | None) -> None:
    source = StandardEbooksSource(safe_parse_xml)
    ref = asyncio.run(source.resolve_url(FakeFetcher(), url))
    assert (ref.remote_id if ref is not None else None) == expected


@pytest.mark.req("EF-SRC-011", "ENF-LEG-003")
def test_without_the_patron_setting_a_feed_operation_is_refused_before_any_request() -> None:
    source = StandardEbooksSource(safe_parse_xml).configured({"auth": "none"})
    fetcher = FakeFetcher()
    feed_ref = SeriesRef(source_key="standardebooks", remote_id="/feeds/opds/all", title="x")
    for call in (
        source.search(fetcher, "x", {}, None),
        source.get_series(fetcher, feed_ref),
        source.list_units(fetcher, feed_ref),
    ):
        with pytest.raises(AuthRequired):
            asyncio.run(call)
    assert fetcher.unscripted == []  # nothing was even tried


@pytest.mark.req("EF-SRC-011", "EF-SRC-013", "ENF-SEC-005")
def test_the_credential_origin_is_the_manifest_s_not_a_setting() -> None:
    """Attack: a setting `base_url` names another origin for the credential; the wrapper is bound
    to the one the manifest declares."""
    wrapped = authenticated(
        object(),
        build_manifest(),
        {"auth": "basic", "username": "lectrice", "base_url": "https://evil.example"},
        {"password": "a-long-enough-secret"},
    )
    assert wrapped._origin == ("https", "standardebooks.org", 443)
