"""M2 L23 (EF-SRC-007, ENF-LEG-003, EF-BIB-049, EF-SRC-024): Project Gutenberg through the official
catalogues and a mirror the admin chose, never through the website. Each "never" of the lot is a
test that tries to break it; nothing here reaches a network."""

from __future__ import annotations

import asyncio
import io
import socket
import zipfile
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from shelfhold.acquisition.public import AcquireRefused
from shelfhold.catalog.public import RightsService
from shelfhold.infrastructure.fetcher import DomainNotAllowed
from shelfhold.library.public import servable_epub
from shelfhold.plugin_host.adapters.native.gutenberg import GutenbergSource
from shelfhold.plugin_host.errors import NoSourceForUrl
from shelfhold.sdk.io_guard import ForbiddenIOError, GuardedPlugin
from tests.gutenberg_env import (
    CATALOG_HOST,
    CATALOG_PATH,
    CATALOG_URL,
    CLEAN,
    KEY,
    MIRROR_HOST,
    MIRROR_URL,
    TRANSLATIONS_RDF,
    TRAPPED,
    Bench,
    epub_path,
    gutenberg_bench,
    host_of,
)


def has_active_content(path: Path) -> bool:
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        return any(b"<script" in archive.read(n) for n in archive.namelist())


def search(b: Bench, query: str) -> list[str]:
    page = b.run(b.acquirer.search(KEY, query))
    return [r.remote_id for r in page.items]


# -- the sources of the files: catalogue and mirror, nothing else -------------------------------


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
def test_gutenberg_uses_rdf_csv_catalog_or_mirror_never_website() -> None:
    """A search reads the catalogue, a book comes from the mirror, and no request ever reaches the
    website: not a page, not a cover, not the robots file of the site."""
    with gutenberg_bench(files={epub_path(1001): CLEAN}) as b:
        assert search(b, "study") == ["1001"]
        _prepared, outcome = b.acquire(1001)
        assert outcome.state == "fetched" and outcome.kind == "epub"
        hosts = {host_of(r) for r in b.wire.recorder.requests}
        assert hosts <= {CATALOG_HOST, MIRROR_HOST} and not b.wire.site
        (catalog_request,) = [r for r in b.wire.to(CATALOG_HOST) if r.url.path != "/robots.txt"]
        assert catalog_request.url.path == CATALOG_PATH  # the official feed path, nothing else
        assert catalog_request.headers["user-agent"].startswith("Shelfhold/")  # identifiable UA
        files = [r.url.path for r in b.wire.to(MIRROR_HOST) if r.url.path != "/robots.txt"]
        assert files == [epub_path(1001)]


@pytest.mark.req("EF-SRC-007")
def test_gutenberg_reads_an_rdf_catalog_too() -> None:
    with gutenberg_bench(wire={"catalog": TRANSLATIONS_RDF}) as b:
        assert sorted(search(b, "count")) == ["7001", "7002"]


@pytest.mark.req("EF-SRC-007")
def test_audio_and_markup_in_the_catalog_are_not_taken_as_books() -> None:
    """A `Sound` row is no book; a title that carries markup is stored as text."""
    with gutenberg_bench(files={epub_path(4001): CLEAN}) as b:
        assert search(b, "audio") == []
        b.acquire(4001)
        (work,) = b.rows("SELECT title FROM work")
        assert "<" not in work["title"] and "script" not in work["title"].lower().split("(")[0]
        assert "Odd" in work["title"]


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
def test_a_pasted_website_url_is_no_source_and_sends_nothing() -> None:
    with gutenberg_bench() as b:
        for url in (
            "https://www.gutenberg.org/ebooks/1001",
            "https://gutenberg.org/cache/epub/1001/pg1001.epub",
        ):
            with pytest.raises(NoSourceForUrl):
                b.run(b.acquirer.resolve(url))
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003", "EF-SRC-003")
@pytest.mark.parametrize(
    "url",
    [
        "https://www.gutenberg.org/ebooks/1001",
        "https://gutenberg.org/",
        "https://www.gutenberg.org/ebooks/search/?query=test",
        "https://aleph.gutenberg.org/1/0/0/1001/1001-h.zip",
    ],
)
def test_a_request_to_the_website_outside_catalogs_and_mirror_is_refused_by_the_domain_rules(
    url: str,
) -> None:
    """The source's own Fetcher refuses it by name, before any resolution and any packet."""
    with gutenberg_bench() as b:
        record = b.registry.get(KEY)
        fetcher = b.fetchers.for_record(record, b.settings)
        with pytest.raises(DomainNotAllowed):
            b.run(fetcher.get(url))
        assert not b.wire.recorder.requests and not b.wire.site


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003", "EF-SRC-002")
def test_a_plugin_that_opens_a_socket_to_the_website_is_caught_by_the_audit_hook() -> None:
    """The DomainRules guard the Fetcher; the audit hook (L05) proves a plugin that skips it: a
    direct connection to the website raises in the kit and is reported with the plugin id."""
    attempts: list[str] = []

    class Rogue(GutenbergSource):
        async def search(self, f: Any, q: str, filters: Any, cursor: Any, **kw: Any) -> Any:
            attempts.append("search")
            socket.getaddrinfo("www.gutenberg.org", 443)  # never reaches a network: refused first
            return await super().search(f, q, filters, cursor, **kw)

    seen: list[Any] = []
    guarded = GuardedPlugin(Rogue(), KEY, report=seen.append, mode="raise")
    with pytest.raises(ForbiddenIOError):
        asyncio.run(guarded.search(None, "study", {}, None))
    assert attempts == ["search"]
    assert [v.plugin_id for v in seen] == [KEY] and "getaddrinfo" in seen[0].event


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
def test_the_website_is_reachable_only_for_the_official_feed_path() -> None:
    """An admin who points the catalogue at the official feed of the website opens that path of
    that host and nothing else on it: no page, no dot-dot, no encoded dot-dot."""
    feed = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
    with gutenberg_bench(settings={"catalog_url": feed, "mirror_url": MIRROR_URL}) as b:
        fetcher = b.fetchers.for_record(b.registry.get(KEY), b.settings)
        for url in (
            "https://www.gutenberg.org/ebooks/1001",
            "https://www.gutenberg.org/cache/epub/feeds/../../../ebooks/1001",
            "https://www.gutenberg.org/cache/epub/feeds/%2e%2e/%2e%2e/ebooks/1001",
            "https://www.gutenberg.org/cache/epub/feeds%2f..%2f..%2febooks/1001",
            "https://www.gutenberg.org/cache/epub/feedsfake/pg_catalog.csv",
        ):
            with pytest.raises(DomainNotAllowed):
                b.run(fetcher.get(url))
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
@pytest.mark.parametrize("on_site_feed", [False, True])
def test_a_mirror_that_redirects_to_the_website_is_refused_at_the_hop(on_site_feed: bool) -> None:
    """The mirror answers the book with a redirection to a page of the website: refused at that
    hop, the request is closed for good, and nothing reaches the website. Even when the admin
    allowed the website's feed path for the catalogue."""
    settings = {
        "catalog_url": (
            "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
            if on_site_feed
            else CATALOG_URL
        ),
        "mirror_url": MIRROR_URL,
    }
    target = "https://www.gutenberg.org/ebooks/1001.epub.noimages"
    with gutenberg_bench(settings=settings, wire={"redirect_files_to": target}) as b:
        search(b, "study")  # the catalogue is read through the allowed feed path
        _prepared, outcome = b.acquire(1001)
        assert outcome.state == "abandoned" and outcome.reason == "source.domain_not_allowed"
        assert not b.wire.site_pages  # at most its robots rules and the feed the admin chose
        assert on_site_feed or not b.wire.site
        assert b.rows("SELECT id FROM content") == []


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
@pytest.mark.parametrize(
    "mirror",
    [
        "https://www.gutenberg.org/",
        "https://gutenberg.org/cache/epub",
        "https://WWW.Gutenberg.org./files",
        "https://user:pw@mirror.example.net/gutenberg",
        "http://127.0.0.1/gutenberg",
        "ftp://mirror.example.net/gutenberg",
        "mirror.example.net/gutenberg",
        "",
    ],
)
def test_a_mirror_that_is_the_website_or_not_a_plain_url_is_refused_before_any_request(
    mirror: str,
) -> None:
    """The mirror is never the website, a URL that holds a credential, or an address that is not
    a plain http(s) one; the source is then simply not configured."""
    with gutenberg_bench(files={epub_path(1001): CLEAN}) as b:
        b.settings["mirror_url"] = mirror
        with pytest.raises(AcquireRefused) as raised:
            b.acquire(1001)
        assert raised.value.code == "source.not_configured"
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-007")
def test_a_source_without_its_mirror_is_not_configured_and_sends_nothing() -> None:
    """`gutenberg.mirror_url` is required to switch the source on (D6): no default host."""
    for settings in ({}, {"catalog_url": CATALOG_URL}, {"mirror_url": MIRROR_URL}):
        with gutenberg_bench(settings=settings) as b:
            with pytest.raises(AcquireRefused) as raised:
                b.run(b.acquirer.search(KEY, "study"))
            assert raised.value.code == "source.not_configured"
            assert not b.wire.recorder.requests


# -- the catalogue: conditional, at most once a week --------------------------------------------


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
def test_the_catalog_is_read_once_a_week_and_revalidated_with_a_conditional_get() -> None:
    week = 7 * 24 * 3600
    with gutenberg_bench() as b:
        search(b, "study")
        search(b, "tale")
        search(b, "count")
        assert len([r for r in b.wire.to(CATALOG_HOST) if r.url.path == CATALOG_PATH]) == 1
        b.time.now += week + 60
        assert search(b, "study") == ["1001"]
        again = [r for r in b.wire.to(CATALOG_HOST) if r.url.path == CATALOG_PATH]
        assert len(again) == 2  # the week is over: asked again, and only once
        assert again[1].headers.get("if-none-match") == b.wire.etag  # conditional, a 304 answers


# -- rate: prudent, and no way around it --------------------------------------------------------


@pytest.mark.req("ENF-LEG-003")
def test_gutenberg_rate_limit_prudent() -> None:
    """The manifest asks for a slow rhythm, and every request of a session keeps it."""
    with gutenberg_bench(files={epub_path(1001): CLEAN, epub_path(3001): CLEAN}) as b:
        limit = b.plugin.manifest.rate_limit
        assert limit.requests_per_second <= 0.5 and limit.concurrency == 1
        search(b, "study")
        b.acquire(1001)
        b.acquire(3001)
        gaps = [later - earlier for earlier, later in pairwise(b.wire.stamps)]
        assert len(b.wire.stamps) >= 5 and min(gaps) >= 1 / limit.requests_per_second - 1e-6


@pytest.mark.req("ENF-LEG-003")
def test_the_rhythm_holds_for_requests_made_at_the_same_time() -> None:
    """Attack: many files asked together, each with a Fetcher of its own, share one rhythm."""
    names = {epub_path(1001): CLEAN, epub_path(3001): CLEAN, epub_path(4001): CLEAN}
    with gutenberg_bench(files=names) as b:
        search(b, "study")

        async def together() -> None:
            await asyncio.gather(
                *(b.acquirer.prepare(b.ref(n)) for n in (1001, 3001, 4001))
            )  # nothing is fetched: only the catalogue is read
            requests = [
                b.acquirer.fetch(str(row["id"]), _store(b), b.place)
                for row in b.rows("SELECT id FROM download_request")
            ]
            await asyncio.gather(*requests)

        b.run(together())
        files = [i for i, r in enumerate(b.wire.recorder.requests) if r.url.path.endswith(".epub")]
        stamps = [b.wire.stamps[i] for i in files]
        assert len(stamps) == 3
        assert all(later - earlier >= 5 - 1e-6 for earlier, later in pairwise(stamps))


@pytest.mark.req("ENF-LEG-003")
def test_a_redirection_hop_and_a_robots_read_count_as_requests_of_the_rhythm() -> None:
    target = f"https://{MIRROR_HOST}/gutenberg/cache/epub/1001/other.epub"
    with gutenberg_bench(wire={"redirect_files_to": target}) as b:
        search(b, "study")
        b.acquire(1001)  # robots.txt, the book, and the hops it is bounced through
        gaps = [later - earlier for earlier, later in pairwise(b.wire.stamps)]
        assert len(b.wire.stamps) >= 3 and min(gaps) >= 5 - 1e-6


@pytest.mark.req("ENF-LEG-003")
def test_a_request_served_from_the_cache_does_not_cost_the_rhythm_but_a_new_one_does() -> None:
    """The cache is the host's (L06): a fresh entry is answered without a request, so it spends
    no slot; what does go out is paced. Nothing lets a request skip the pacer."""
    with gutenberg_bench() as b:
        search(b, "study")
        before = len(b.wire.stamps)
        search(b, "tale")
        assert len(b.wire.stamps) == before  # nothing went out
        slept = list(b.time.slept)
        b.time.now += 3600  # an hour later: the first request of a new operation waits for nothing
        record = b.registry.get(KEY)
        fetcher = b.fetchers.for_record(record, b.settings)
        b.run(fetcher.get(f"https://{MIRROR_HOST}/gutenberg/cache/epub/1/pg1.epub"))
        # the robots rules of the mirror went at once, the book then waited one slot, no more
        assert b.time.slept == [*slept, 5.0]


# -- the file: the untrusted chain, always ------------------------------------------------------


@pytest.mark.req("EF-BIB-051", "EF-SRC-007")
def test_an_epub_from_the_mirror_is_neutralized_whatever_the_manifest_or_the_name() -> None:
    """Attack: the mirror serves an EPUB with a script. The binding is `untrusted`, the neutralized
    derivative is what is exported, and the original stays in /store."""
    with gutenberg_bench(files={epub_path(1001): TRAPPED}) as b:
        assert b.plugin.manifest.content_trust == "untrusted"
        prepared, outcome = b.acquire(1001)
        assert outcome.state == "fetched"
        (binding,) = b.rows("SELECT content_trust FROM source_binding")
        assert binding["content_trust"] == "untrusted"
        (library,) = b.lib.files_under(b.lib.manga)
        assert not has_active_content(library) and b.lib.files_under(b.lib.store)
        assert Path(servable_epub(b.lib.engine, b.lib.fsops, prepared.unit_id).path) == library


@pytest.mark.req("EF-BIB-051", "EF-SRC-007")
def test_a_book_that_is_not_an_epub_is_not_filed_as_one() -> None:
    """Attack: the mirror serves something else under the `.epub` name; the bytes decide."""
    fake = b"MZ\x90\x00 this is not an archive"
    with gutenberg_bench(files={epub_path(1001): fake}) as b:
        _prepared, outcome = b.acquire(1001)
        assert outcome.kind != "epub"  # whatever it is, the name did not make it an EPUB


@pytest.mark.req("EF-SRC-007", "EF-SRC-015")
def test_a_book_the_mirror_does_not_have_waits_or_closes_without_touching_the_website() -> None:
    with gutenberg_bench(files={}) as b:
        _prepared, outcome = b.acquire(1001)
        assert outcome.state in ("abandoned", "waiting") and not b.wire.site


# -- what the catalogue gives the library -------------------------------------------------------


@pytest.mark.req("EF-SRC-007", "EF-SRC-024")
def test_gutenberg_book_creates_novel_work_with_us_public_domain_basis() -> None:
    with gutenberg_bench(files={epub_path(1001): CLEAN}) as b:
        prepared, outcome = b.acquire(1001)
        assert outcome.state == "fetched"
        (work,) = b.rows("SELECT id, kind, title FROM work")
        assert work["kind"] == "novel" and work["title"] == "A Study in Test"
        (ids,) = b.rows(
            "SELECT provider, value, entity_type FROM external_id WHERE provider = 'gutenberg'"
        )
        assert (ids["entity_type"], ids["value"]) == ("work", "1001")
        (edition,) = b.rows(
            "SELECT lang, media, publisher, translation_type, rights_basis, rights_jurisdiction "
            "FROM edition"
        )
        assert edition == {
            "lang": "en", "media": "text", "publisher": "Project Gutenberg",
            "translation_type": "official", "rights_basis": "us_public_domain",
            "rights_jurisdiction": "US",
        }  # fmt: skip
        credit = b.rows(
            "SELECT c.primary_name, c.death_date FROM contribution n "
            "JOIN contributor c ON c.id = n.contributor_id WHERE n.role = 'aut'"
        )
        assert credit == [{"primary_name": "Doe, Jane", "death_date": "1920"}]
        assert prepared.created


@pytest.mark.req("EF-BIB-049", "EF-SRC-007")
def test_gutenberg_translations_published_two_editions() -> None:
    """Two published translations of one novel, by two translators in one language, are two
    Editions of one Work (the translator is the Edition's text version)."""
    files = {epub_path(7001): CLEAN, epub_path(7002): CLEAN}
    with gutenberg_bench(wire={"catalog": TRANSLATIONS_RDF}, files=files) as b:
        b.acquire(7001)
        b.acquire(7002)
        assert len(b.rows("SELECT id FROM work")) == 1
        editions = b.rows("SELECT text_version, translation_type, rights_basis FROM edition")
        assert len(editions) == 2 and len({e["text_version"] for e in editions}) == 2
        assert all(e["translation_type"] == "official" for e in editions)
        values = {
            r["value"] for r in b.rows("SELECT value FROM external_id WHERE provider = 'gutenberg'")
        }
        assert values == {"7001", "7002"}


# -- the rights: US public domain is not the user's public domain -------------------------------


@pytest.mark.req("EF-SRC-024", "ENF-LEG-008", "EF-SRC-007")
def test_a_us_public_domain_book_added_outside_the_us_is_not_taken_as_free() -> None:
    """Attack: a book marked "public domain in the US" is added on an instance whose country is
    not the US and was never confirmed. It is added (the warning never blocks, EF-BIB-046), but
    the mark stays "US", the calculation is made for the instance's country, and a work that may
    still be protected there says so. The instance's country is never rewritten."""
    with gutenberg_bench(files={epub_path(3001): CLEAN, epub_path(8001): CLEAN}) as b:
        b.set_jurisdiction("FR", confirmed=False)
        _prepared, outcome = b.acquire(3001)
        assert outcome.state == "fetched"  # not blocked
        (edition,) = b.rows("SELECT id, rights_basis, rights_jurisdiction FROM edition")
        assert (edition["rights_basis"], edition["rights_jurisdiction"]) == (
            "us_public_domain",
            "US",
        )
        assert [w.payload["reason"] for w in b.warnings] == ["may_be_protected"]  # Roe: 1960 + 70
        with b.lib.engine.connect() as connection:
            report = RightsService(connection).assess(edition["id"], today=date(2026, 11, 11))
        assert report.jurisdiction == "FR" and report.confirmed is False
        assert report.verdict.value == "may_be_protected"
        assert b.rows("SELECT jurisdiction FROM instance")[0]["jurisdiction"] == "FR"


@pytest.mark.req("EF-SRC-024", "ENF-LEG-008")
def test_the_us_basis_alone_never_says_there_is_nothing_to_warn_of_outside_the_us() -> None:
    """A book whose catalogue gives no death year: outside the US the answer is "no information",
    never "no warning" on the strength of the US mark."""
    with gutenberg_bench(files={epub_path(8001): CLEAN}) as b:
        b.set_jurisdiction("FR", confirmed=True)
        b.acquire(8001)
        (edition,) = b.rows("SELECT id FROM edition")
        with b.lib.engine.connect() as connection:
            report = RightsService(connection).assess(edition["id"], today=date(2026, 11, 11))
        assert report.verdict.value == "no_information" and not b.warnings


@pytest.mark.req("EF-SRC-024")
def test_a_book_with_no_warning_in_the_instance_country_says_nothing() -> None:
    with gutenberg_bench(files={epub_path(1001): CLEAN}) as b:
        b.set_jurisdiction("FR", confirmed=True)
        b.acquire(1001)
        assert not b.warnings  # Doe died in 1920: no warning in France either


def _store(b: Bench) -> Any:
    from tests.gutenberg_env import RoutedStore

    return RoutedStore(b.lib)
