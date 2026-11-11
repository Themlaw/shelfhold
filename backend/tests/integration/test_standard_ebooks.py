"""M2 L24 (EF-SRC-011, EF-BIB-033, EF-BIB-051, ENF-LEG-003, ENF-LEG-008, EF-SRC-013): Standard
Ebooks, one URL per book, and the patron OPDS feed only with the patron's credentials. Each "never"
of the lot is a test that tries to break it; nothing here reaches a network."""

from __future__ import annotations

import asyncio
import base64
import io
import zipfile
from itertools import pairwise
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx2
import pytest
import structlog

from shelfhold.infrastructure.fetcher import DomainNotAllowed
from shelfhold.library.public import servable_epub
from shelfhold.plugin_host.errors import NoSourceForUrl
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import AuthRequired
from tests.fakes.standardebooks import FEED_ROOT, download_path
from tests.gutenberg_env import CLEAN, TRAPPED, RoutedStore
from tests.standardebooks_env import (
    EVIL,
    HOST,
    KEY,
    SLUG,
    SLUG2,
    Bench,
    basic,
    files_text,
    host_of,
    standardebooks_bench,
)


def search(b: Bench, query: str = "test") -> list[SeriesRef]:
    return b.run(b.acquirer.search(KEY, query)).items


# -- one URL per book ---------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-011", "EF-SRC-004")
def test_standard_ebooks_book_url() -> None:
    """A pasted book address gives the book, and the book is the one request to its own download:
    no page of the site, no feed, no listing."""
    with standardebooks_bench() as b:
        b.serve_book(SLUG, CLEAN)
        ref = b.run(b.acquirer.resolve(f"https://{HOST}/ebooks/{SLUG}"))
        assert ref.source_key == KEY and ref.remote_id == SLUG
        assert not b.wire.recorder.requests  # naming a book asks nothing
        _prepared, outcome = b.acquire(ref=ref)
        assert outcome.state == "fetched" and outcome.kind == "epub"
        assert {host_of(r) for r in b.wire.recorder.requests} == {HOST}
        calls = [r.url.path for r in b.wire.recorder.requests if r.url.path != "/robots.txt"]
        assert calls == [download_path(SLUG)]  # one request, to the file of that book
        assert b.wire.recorder.requests[-1].headers["user-agent"].startswith("Shelfhold/")
        assert not b.feed_calls()


@pytest.mark.req("EF-SRC-011", "ENF-LEG-003")
@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/ebooks/jane-synth",  # an author's listing: harvesting
        f"https://{HOST}/ebooks",
        f"https://{HOST}/ebooks/",
        f"https://{HOST}/ebooks?page=2",
        f"https://{HOST}/collections/a-collection",
        f"https://{HOST}/subjects/fiction",
        f"https://{HOST}/feeds/opds",
        f"https://{HOST}/feeds/opds/all",
        f"https://{HOST}/ebooks/{SLUG}/downloads/jane-synth_le-test.epub",
        f"https://{HOST}/ebooks/{SLUG}/text/chapter-1",
        f"https://{HOST}/ebooks/jane-synth/le-test/a/b/c",
        f"https://{HOST}.evil.example/ebooks/{SLUG}",
        f"https://evil.example/{HOST}/ebooks/{SLUG}",
        f"https://user:pw@{HOST}/ebooks/{SLUG}",
        f"https://{HOST}:8443/ebooks/{SLUG}",
        f"https://www.{HOST}/ebooks/{SLUG}",
    ],
)
def test_a_url_that_is_not_one_book_is_no_source_and_sends_nothing(url: str) -> None:
    """Attack: a listing, a feed, a part of a book or a look-alike host pasted as an address."""
    with standardebooks_bench() as b:
        with pytest.raises(NoSourceForUrl):
            b.run(b.acquirer.resolve(url))
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-011", "ENF-SEC-014")
@pytest.mark.parametrize(
    "remote_id",
    [
        "../etc/passwd",
        "jane-synth/../le-test",
        "jane-synth/le-test/../../x",
        "Jane-Synth/Le-Test",
        "jane-synth",
        "jane-synth/le-test/a/b",
        "jane-synth//le-test",
        "jane-synth/le-test?x=1",
        "jane-synth/le-test#x",
        "jane-synth/le-test\n",
        "jane-synth/%2e%2e/x",
        "/feeds/opds/all",
        "x" * 300,
    ],
)
def test_a_reference_that_is_not_a_book_path_is_refused_without_a_request(remote_id: str) -> None:
    """Attack: a stored or forged reference tries to steer the download. Without patron mode a
    reference is a plain `author/title` path and nothing else, and nothing is sent."""
    with standardebooks_bench() as b:
        with pytest.raises(Exception):  # noqa: B017 (a refusal, whatever its type)
            b.run(b.acquirer.prepare(SeriesRef(source_key=KEY, remote_id=remote_id, title="")))
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-011", "ENF-LEG-008", "EF-BIB-033")
def test_a_standard_ebooks_book_is_free_of_rights_in_the_us_with_its_origin_attribution() -> None:
    with standardebooks_bench() as b:
        b.serve_book(SLUG, CLEAN)
        b.acquire()
        (edition,) = b.rows("SELECT rights, rights_basis, rights_jurisdiction FROM edition")
        assert edition["rights_basis"] == "us_public_domain"
        assert edition["rights_jurisdiction"] == "US"
        assert "CC0" in edition["rights"] and f"https://{HOST}/ebooks/{SLUG}" in edition["rights"]
        (work,) = b.rows("SELECT title FROM work")
        assert work["title"] == "Le Test"


@pytest.mark.req("EF-SRC-011", "EF-SRC-003", "ENF-LEG-003")
@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/",
        f"https://{HOST}/blog",
        f"https://{HOST}/collections/a-collection",
        f"https://{HOST}/ebooks/../blog",
        f"https://{HOST}/ebooks/%2e%2e/blog",
        f"https://{HOST}/feeds/../blog",
        f"https://{EVIL}/ebooks/{SLUG}",
        "https://github.com/standardebooks/x",
    ],
)
def test_a_request_outside_the_domain_rules_is_refused_before_any_packet(url: str) -> None:
    with standardebooks_bench() as b:
        fetcher = b.fetchers.for_record(b.registry.get(KEY), b.settings)
        with pytest.raises(DomainNotAllowed):
            b.run(fetcher.get(url))
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-011", "EF-SRC-003")
def test_a_download_that_bounces_to_another_host_is_refused_at_the_hop() -> None:
    with standardebooks_bench() as b:
        b.wire.serve(
            download_path(SLUG),
            lambda _r: httpx2.Response(302, headers={"location": f"https://{EVIL}/x.epub"}),
        )
        _prepared, outcome = b.acquire()
        assert outcome.kind != "epub" and not b.wire.sent_to(EVIL)


# -- the patron feed: only with the patron's credentials ----------------------------------------


@pytest.mark.req("EF-SRC-011", "ENF-LEG-003")
def test_standard_ebooks_patron_feed_only_with_credentials() -> None:
    """Without credentials no request goes to the reserved feed, whatever is asked: a search, a
    reference that names a feed, a series read. With them the feed is read, with the credential."""
    with standardebooks_bench(auth="none") as b:
        b.serve_patron([SLUG])
        with pytest.raises(AuthRequired):
            search(b)
        feed_ref = SeriesRef(source_key=KEY, remote_id=f"{FEED_ROOT}/all", title="x")
        with pytest.raises(Exception):  # noqa: B017
            b.run(b.acquirer.prepare(feed_ref))
        assert not b.wire.recorder.requests and not b.feed_calls()
    with standardebooks_bench(auth="basic") as b:
        b.serve_patron([SLUG, SLUG2])
        b.serve_book(SLUG, CLEAN)
        refs = search(b)
        assert len(refs) == 2 and {r.source_key for r in refs} == {KEY}
        assert b.feed_calls()
        secret = b.secrets["password"]
        for request in b.feed_calls():
            assert request.headers["authorization"] == basic("lectrice", secret)
        _prepared, outcome = b.acquire(ref=refs[0])
        assert outcome.state == "fetched" and outcome.kind == "epub"


@pytest.mark.req("EF-SRC-011", "ENF-LEG-003")
def test_a_patron_setting_without_its_secret_sends_nothing() -> None:
    """Attack: `auth` says basic and the secret was never set: no request goes out unauthenticated,
    the reserved feed is not asked anonymously."""
    with standardebooks_bench(auth="basic") as b:
        b.secrets.pop("password")
        b.serve_patron([SLUG])
        with pytest.raises(AuthRequired):
            search(b)
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-011", "ENF-LEG-003")
def test_the_per_book_path_never_touches_the_feed_even_with_credentials() -> None:
    with standardebooks_bench(auth="basic") as b:
        b.serve_book(SLUG, CLEAN)
        b.acquire()  # a plain `author/title` reference stays one download
        assert not b.feed_calls()
        assert [r.url.path for r in b.book_calls()] == [download_path(SLUG)]


@pytest.mark.req("EF-SRC-011", "ENF-LEG-003")
def test_the_patron_feed_is_never_walked_beyond_what_is_asked() -> None:
    """Attack: a feed that always has a next page. A search reads the root, the description and one
    page of results and hands back a cursor; acquiring a book reads that page again, never the
    next one, and no listing of the site is read."""
    with standardebooks_bench(auth="basic") as b:
        b.serve_patron([SLUG], next_href=f"{FEED_ROOT}/all2")
        b.serve_book(SLUG, CLEAN)
        page = b.run(b.acquirer.search(KEY, "test"))
        assert page.next is not None
        asked = {r.url.path for r in b.feed_calls()}
        assert asked == {FEED_ROOT, f"{FEED_ROOT}/search", f"{FEED_ROOT}/all"}
        b.acquire(ref=page.items[0])
        assert f"{FEED_ROOT}/all2" not in {r.url.path for r in b.feed_calls()}
        assert all(r.url.path.startswith(("/feeds/", "/ebooks/", "/robots")) for r in
                   b.wire.recorder.requests)  # fmt: skip


@pytest.mark.req("EF-SRC-011", "EF-SRC-013", "ENF-SEC-005")
def test_the_patron_credential_never_leaks_into_journal_reference_cache_or_database() -> None:
    with standardebooks_bench(auth="basic") as b, structlog.testing.capture_logs() as logs:
        b.serve_patron([SLUG])
        b.serve_book(SLUG, CLEAN)
        refs = search(b)
        b.acquire(ref=refs[0])
        secret = b.secrets["password"]
        forms = {
            secret,
            quote(secret, safe=""),
            quote_plus(secret),
            base64.b64encode(f"lectrice:{secret}".encode()).decode(),
        }
        cache = b.fetchers.cache_of(KEY)
        cached = repr(cache._entries if cache is not None else {})
        shown = repr([r.model_dump() for r in refs]) + repr(b.acquirer) + repr(b.fetchers)
        stored = b.database_text() + files_text(b.lib.tmp)
        journal = repr(logs)
        for form in forms:
            assert form not in journal, "journal"
            assert form not in stored, "database"
            assert form not in cached, "cache"
            assert form not in shown, "reference"
        rows = b.rows("SELECT remote_id FROM source_item")
        assert rows and all("lectrice" not in r["remote_id"] for r in rows)


@pytest.mark.req("EF-SRC-011", "EF-SRC-013", "ENF-SEC-005")
def test_an_error_of_the_feed_names_no_credential() -> None:
    with standardebooks_bench(auth="basic") as b, structlog.testing.capture_logs() as logs:
        for path in (FEED_ROOT, f"{FEED_ROOT}/search", f"{FEED_ROOT}/all"):
            b.wire.serve(path, lambda _r: httpx2.Response(500, content=b"boom"))
        with pytest.raises(Exception) as caught:
            search(b)
        text = repr(caught.value) + str(caught.value) + repr(logs)
        secret = b.secrets["password"]
        assert (
            secret not in text
            and base64.b64encode(f"lectrice:{secret}".encode()).decode() not in text
        )


@pytest.mark.req("EF-SRC-011", "EF-SRC-013", "ENF-SEC-005")
def test_the_credential_goes_to_the_site_only_whatever_the_settings_or_the_feed_say() -> None:
    """Attack 1: a setting names another address for the credential. Attack 2: the feed offers a
    book on another host. The credential reaches `standardebooks.org` and nothing else."""
    with standardebooks_bench(auth="basic", extra_settings={"base_url": f"https://{EVIL}"}) as b:
        b.serve_patron([SLUG])
        search(b)
        assert not b.wire.sent_to(EVIL) and b.feed_calls()
        secret = b.secrets["password"]
        assert all(r.headers["authorization"] == basic("lectrice", secret) for r in b.feed_calls())
    with standardebooks_bench(auth="basic") as b:
        from tests.fakes.opds import ATOM, acquisition_entry, atom_feed
        from tests.opds_env import document

        elsewhere = atom_feed(
            "urn:test:x", "Résultats",
            [acquisition_entry("urn:test:e", "Livre", f"https://{EVIL}/x.epub")],
        )  # fmt: skip
        b.serve_patron([SLUG])
        b.wire.serve(f"{FEED_ROOT}/all", document(elsewhere, ATOM))
        refs = search(b)
        assert refs  # the entry is listed, its file is not on this site
        with pytest.raises(Exception):  # noqa: B017 (a refusal, whatever its type)
            b.run(b.acquirer.prepare(refs[0]))
        assert not b.wire.sent_to(EVIL)


# -- the untrusted chain, always ----------------------------------------------------------------


@pytest.mark.req("EF-BIB-051", "EF-SRC-011")
def test_a_standard_ebooks_epub_with_active_content_is_neutralized_and_its_original_kept() -> None:
    """Attack: the file carries a script. The binding is `untrusted` whatever the site is, the
    neutralized derivative is what is served, the original stays in /store."""
    with standardebooks_bench(extra_settings={"content_trust": "publisher"}) as b:
        assert b.registry.get(KEY).manifest.content_trust == "untrusted"
        b.serve_book(SLUG, TRAPPED)
        prepared, outcome = b.acquire()
        assert outcome.state == "fetched"
        (binding,) = b.rows("SELECT content_trust FROM source_binding")
        assert binding["content_trust"] == "untrusted"
        (library,) = b.lib.files_under(b.lib.manga)
        with zipfile.ZipFile(io.BytesIO(library.read_bytes())) as archive:
            assert not any(b"<script" in archive.read(n) for n in archive.namelist())
        assert b.lib.files_under(b.lib.store)
        assert Path(servable_epub(b.lib.engine, b.lib.fsops, prepared.unit_id).path) == library


@pytest.mark.req("EF-SRC-011")
def test_a_download_that_is_not_an_epub_is_not_filed_as_one() -> None:
    with standardebooks_bench() as b:
        b.serve_book(SLUG, b"<html>not a book</html>")
        _prepared, outcome = b.acquire()
        assert outcome.kind != "epub" and not b.lib.files_under(b.lib.manga)


# -- the rhythm ---------------------------------------------------------------------------------


@pytest.mark.req("ENF-LEG-003", "EF-SRC-011")
def test_the_rhythm_is_slow_and_holds_for_books_asked_at_the_same_time() -> None:
    """Attack: two books asked together, each with a Fetcher of its own, share one rhythm."""
    with standardebooks_bench() as b:
        limit = b.registry.get(KEY).manifest.rate_limit
        assert limit.requests_per_second <= 0.5 and limit.concurrency == 1
        for slug in (SLUG, SLUG2):
            b.serve_book(slug, CLEAN)

        async def together() -> None:
            prepared = await asyncio.gather(*(b.acquirer.prepare(b.ref(s)) for s in (SLUG, SLUG2)))
            await asyncio.gather(
                *(
                    b.acquirer.fetch(str(p.request_id), RoutedStore(b.lib), b.place)
                    for p in prepared
                )
            )

        b.run(together())
        files = [i for i, r in enumerate(b.wire.recorder.requests) if r.url.path.endswith(".epub")]
        stamps = [b.wire.stamps[i] for i in files]
        assert len(stamps) == 2
        assert all(later - earlier >= 1 / limit.requests_per_second - 1e-6
                   for earlier, later in pairwise(stamps))  # fmt: skip
