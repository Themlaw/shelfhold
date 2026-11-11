"""M2 L22 (EF-SRC-006, EF-SRC-023, EF-BIB-051, ENF-LEG-007, ENF-SEC-005): the generic OPDS source
through the one-shot acquisition, over the real Fetcher, registry, catalog and stores, against
scripted Kavita-shaped and Komga-shaped servers. Every "never" is a test that tries to break it."""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from urllib.parse import quote, quote_plus

import httpx2
import pytest
import structlog

from shelfhold.acquisition.public import AcquireRefused
from shelfhold.library.public import servable_epub
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import SourceError
from shelfhold.sdk.testing.corpus.trapped_epubs.build import epub_bytes, xhtml
from tests.fakes.opds import ATOM, acquisition_entry, atom_feed
from tests.opds_env import (
    EVIL,
    HOST,
    KEY,
    Bench,
    document,
    file,
    new_key,
    opds_bench,
    redirect,
    serve_kavita,
    serve_komga,
)

CLEAN = epub_bytes(xhtml("<p>chapitre</p>"))
TRAPPED = epub_bytes(xhtml("<p>chapitre</p><script>alert(1)</script>"))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def search(b: Bench, terms: str = "titre") -> list[SeriesRef]:
    page = run(b.acquirer.search(KEY, terms))
    return list(page.items)


def acquire(b: Bench, ref: SeriesRef | None = None, unit: str | None = None) -> Any:
    async def go() -> Any:
        found = ref or (await b.acquirer.search(KEY, "titre")).items[0]
        prepared = await b.acquirer.prepare(found, unit=unit)
        assert prepared.request_id is not None
        outcome = await b.acquirer.fetch(prepared.request_id, b.store(), b.place)
        return prepared, outcome

    return run(go())


def basic_header(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# -- the two flavours, end to end -------------------------------------------------------------


@pytest.mark.req("EF-SRC-006", "EF-SRC-023")
def test_opds_komga_basic_search_and_acquire() -> None:
    with opds_bench(auth="basic", base_path="/opds") as b:
        expected = basic_header("lecteur", b.secrets["password"])
        serve_komga(
            b, {"tome-1.epub": CLEAN}, expect=lambda r: r.headers.get("authorization") == expected
        )
        (ref,) = search(b)
        assert ref.source_key == KEY and ref.title == "Un livre de test"
        _prepared, outcome = acquire(b, ref)
        assert outcome.state == "fetched" and outcome.kind == "epub"
        # the OpenSearch template was filled with the term, quoted, and the credential went along
        search_calls = [r for r in b.wire.recorder.requests if r.url.path.endswith("/v1.2/series")]
        assert search_calls and search_calls[0].url.params["search"] == "titre"
        assert all(
            r.headers.get("authorization") == expected
            for r in b.wire.recorder.requests
            if r.url.path != "/robots.txt"
        )
        (content,) = b.rows("SELECT format FROM content")
        assert content["format"] == "epub"


@pytest.mark.req("EF-SRC-006", "EF-SRC-023")
def test_opds_kavita_api_key_in_the_path_search_and_acquire() -> None:
    with opds_bench(auth="api_key_path", base_path="/api/opds/{apiKey}") as b:
        serve_kavita(b, {"livre.epub": CLEAN})
        (ref,) = search(b)
        # the key is a segment of every Kavita link; the stored reference holds the placeholder
        assert b.secrets["api_key"] not in ref.remote_id and "{secret}" in ref.remote_id
        _prepared, outcome = acquire(b, ref)
        assert outcome.state == "fetched"
        paths = [r.url.path for r in b.wire.recorder.requests if r.url.path != "/robots.txt"]
        assert paths and all(p.startswith(f"/api/opds/{b.secrets['api_key']}") for p in paths)


@pytest.mark.req("EF-SRC-006")
def test_opds_without_credentials_keeps_working() -> None:
    with opds_bench(auth="none", base_path="/api/opds/open") as b:
        serve_kavita(b, {"livre.epub": CLEAN}, key="open")
        _prepared, outcome = acquire(b)
        assert outcome.state == "fetched"
        assert all("authorization" not in r.headers for r in b.wire.recorder.requests)


@pytest.mark.req("EF-SRC-006")
def test_opds_acquisition_of_a_series_with_several_books_needs_a_choice() -> None:
    with opds_bench(auth="api_key_path", base_path="/api/opds/{apiKey}") as b:
        serve_kavita(b, {"un.epub": CLEAN, "deux.epub": epub_bytes(xhtml("<p>deux</p>"))})
        (ref,) = search(b)
        with pytest.raises(AcquireRefused) as caught:
            run(b.acquirer.prepare(ref))
        assert caught.value.code == "acquire.unit_required"
        _prepared, outcome = acquire(b, ref, unit="urn:test:book:42:2")
        assert outcome.state == "fetched"


# -- credentials -------------------------------------------------------------------------------

MODES = ("basic", "api_key_header", "api_key_query", "api_key_path")


def setup_for(b: Bench, mode: str) -> None:
    if mode == "basic":
        serve_komga(b, {"tome-1.epub": CLEAN})
    elif mode == "api_key_path":
        serve_kavita(b, {"livre.epub": CLEAN})
    else:  # a header or a query key on a Komga-shaped tree
        serve_komga(b, {"tome-1.epub": CLEAN})


def bench_for(mode: str) -> Any:
    base = "/api/opds/{apiKey}" if mode == "api_key_path" else "/opds"
    return opds_bench(auth=mode, base_path=base)


def forms_of(b: Bench) -> set[str]:
    """Every form the credential can take on its way: raw, percent-encoded, base64."""
    forms: set[str] = set()
    for value in b.secrets.values():
        forms |= {value, quote(value, safe=""), quote_plus(value)}
    if "password" in b.secrets:
        forms.add(
            base64.b64encode(f"{b.settings['username']}:{b.secrets['password']}".encode()).decode()
        )
    return {f for f in forms if len(f) >= 8}


@pytest.mark.req("EF-SRC-023")
def test_opds_authflow_basic_and_api_key_reach_the_wire_in_their_place() -> None:
    """Extends `test_authflow_basic_and_api_key` to the whole acquisition: the feed, the OpenSearch
    description, the search and the file all carry the credential, in the place the flow says."""
    for mode in MODES:
        with bench_for(mode) as b:
            setup_for(b, mode)
            acquire(b)
            calls = [r for r in b.wire.recorder.requests if r.url.path != "/robots.txt"]
            assert len(calls) >= 4, mode
            secret = b.secrets.get("password") or b.secrets["api_key"]
            for r in calls:
                if mode == "basic":
                    assert r.headers["authorization"] == basic_header("lecteur", secret)
                elif mode == "api_key_header":
                    assert r.headers["x-api-key"] == secret
                elif mode == "api_key_query":
                    assert r.url.params["apikey"] == secret
                else:
                    assert f"/{secret}/" in f"{r.url.path}/" or r.url.path.endswith(secret)


@pytest.mark.req("EF-SRC-023", "ENF-SEC-005")
def test_opds_credentials_never_leak_into_journal_reference_cache_or_database() -> None:
    for mode in MODES:
        with bench_for(mode) as b, structlog.testing.capture_logs() as logs:
            setup_for(b, mode)
            refs = search(b)
            acquire(b, refs[0])
            forms = forms_of(b)
            assert forms
            journal = repr(logs)
            stored = b.database_text()
            cached = repr(b.fetchers.cache_of(KEY)._entries if b.fetchers.cache_of(KEY) else {})
            shown = repr([r.model_dump() for r in refs]) + repr(b.acquirer) + repr(b.fetchers)
            for form in forms:
                assert form not in journal, (mode, "journal")
                assert form not in stored, (mode, "database")
                assert form not in cached, (mode, "cache")
                assert form not in shown, (mode, "reference")
            # and the placeholder is what a path key becomes in a stored reference
            if mode == "api_key_path":
                rows = b.rows("SELECT remote_id FROM source_item")
                assert rows and all("{secret}" in r["remote_id"] for r in rows)


@pytest.mark.req("EF-SRC-023", "ENF-SEC-005")
def test_opds_error_messages_never_carry_the_credential() -> None:
    """A server that answers 500 or an HTML page: what is raised and logged names no credential."""
    for mode in MODES:
        with bench_for(mode) as b, structlog.testing.capture_logs() as logs:
            for path in ("/opds", "/api/opds/" + b.secrets.get("api_key", "x")):
                b.wire.serve(path, lambda _r: httpx2.Response(500, content=b"boom"))
            with pytest.raises(Exception) as caught:
                search(b)
            text = repr(caught.value) + str(caught.value) + repr(logs)
            for form in forms_of(b):
                assert form not in text, mode


@pytest.mark.req("EF-SRC-023", "ENF-LEG-007")
def test_opds_authenticated_requests_are_never_cached() -> None:
    for mode in MODES:
        with bench_for(mode) as b:
            setup_for(b, mode)
            search(b)
            first = len(b.wire.recorder.requests)
            search(b)
            assert len(b.wire.recorder.requests) > first, mode  # asked again: nothing was kept
            cache = b.fetchers.cache_of(KEY)
            assert cache is None or not cache._entries, mode


@pytest.mark.req("ENF-LEG-007")
def test_opds_public_feed_is_cached_so_the_previous_test_means_something() -> None:
    with opds_bench(auth="none", base_path="/api/opds/open") as b:
        serve_kavita(b, {"livre.epub": CLEAN}, key="open")
        search(b)
        first = len(b.wire.recorder.requests)
        search(b)
        assert len(b.wire.recorder.requests) == first


@pytest.mark.req("EF-SRC-023")
def test_opds_userinfo_in_the_address_is_refused() -> None:
    """Attack: `user:password@` in the address, a plain setting stored in clear: refused."""
    with opds_bench(auth="none", base_path="/opds") as b:
        b.settings["base_url"] = f"https://lecteur:{new_key()}@{HOST}/opds"
        with pytest.raises((AcquireRefused, SourceError)):
            search(b)
        assert b.wire.recorder.requests == []


# -- redirections and hosts --------------------------------------------------------------------


@pytest.mark.req("EF-SRC-023", "ENF-LEG-007")
def test_opds_file_redirected_to_another_host_never_carries_the_credential() -> None:
    for mode in ("basic", "api_key_header"):
        with bench_for(mode) as b:
            serve_komga(b, {"tome-1.epub": CLEAN})
            b.wire.serve(
                "/opds/v1.2/books/1/file/tome-1.epub", redirect(f"https://{EVIL}/steal.epub")
            )
            b.wire.serve("/steal.epub", file(CLEAN))
            _prepared, outcome = acquire(b)
            assert outcome.state == "abandoned", mode  # off the DomainRules: refused by name
            assert b.wire.sent_to(EVIL) == [], mode  # not even a request, so no header


@pytest.mark.req("EF-SRC-023", "ENF-LEG-007")
def test_opds_file_redirected_to_another_origin_of_the_same_host_loses_the_credential() -> None:
    """Same host name, other port: allowed by name, another origin: `Authorization`, the API key
    header and the query key do not follow."""
    for mode in ("basic", "api_key_header", "api_key_query"):
        with bench_for(mode) as b:
            serve_komga(b, {"tome-1.epub": CLEAN})
            b.wire.serve(
                "/opds/v1.2/books/1/file/tome-1.epub",
                redirect(f"https://{HOST}:8443/second/tome-1.epub"),
            )
            b.wire.serve("/second/tome-1.epub", file(CLEAN))
            acquire(b)
            (second,) = [
                r
                for r in b.wire.recorder.requests
                if r.url.port == 8443 and r.url.path != "/robots.txt"
            ]
            sent = f"{second.url}\n{dict(second.headers)}"
            for form in forms_of(b):
                assert form not in sent, mode
            assert "authorization" not in second.headers and "x-api-key" not in second.headers


@pytest.mark.req("EF-SRC-023", "ENF-LEG-007")
def test_opds_acquisition_link_on_another_host_is_refused_before_any_request() -> None:
    for mode in ("basic", "api_key_path"):
        with bench_for(mode) as b:
            if mode == "basic":
                root = "/opds"
                base = f"{root}/v1.2"
            else:
                root = f"/api/opds/{b.secrets['api_key']}"
                base = root
            entry = acquisition_entry("urn:test:b", "Un livre", f"https://{EVIL}/f/b.epub")
            b.wire.serve(root, document(atom_feed("urn:r", "R", [entry]), ATOM))
            b.wire.serve(f"{base}/x", document(atom_feed("urn:r", "R", [entry]), ATOM))
            ref = SeriesRef(source_key=KEY, remote_id=root, title="Un livre")
            with pytest.raises((AcquireRefused, SourceError)):
                run(b.acquirer.prepare(ref, unit="urn:test:b"))
            assert b.wire.sent_to(EVIL) == []


@pytest.mark.req("EF-SRC-006", "ENF-LEG-007")
def test_opensearch_outside_the_domain_rules_is_refused() -> None:
    """The description or the template names another host: no request goes there, and the
    credential does not leave."""
    for mode in ("basic", "api_key_path"):
        with bench_for(mode) as b:
            root = "/opds" if mode == "basic" else f"/api/opds/{b.secrets['api_key']}"
            from tests.fakes.opds import OPENSEARCH, kavita_description
            from tests.fakes.opds.feeds import link

            # (1) the template of the description names another host
            b.wire.serve(
                root,
                document(
                    atom_feed("urn:r", "R", [], links=[link("search", f"{root}/s", OPENSEARCH)]),
                    ATOM,
                ),
            )
            b.wire.serve(
                f"{root}/s",
                document(kavita_description(f"https://{EVIL}/s?q={{searchTerms}}"), OPENSEARCH),
            )
            with pytest.raises((AcquireRefused, SourceError)):
                search(b)
            # (2) the search link itself names another host
            b.wire.serve(
                root,
                document(
                    atom_feed(
                        "urn:r", "R", [], links=[link("search", f"https://{EVIL}/s", OPENSEARCH)]
                    ),
                    ATOM,
                ),
            )
            with pytest.raises((AcquireRefused, SourceError)):
                search(b)
            assert b.wire.sent_to(EVIL) == [], mode


@pytest.mark.req("EF-SRC-006", "ENF-LEG-007")
def test_a_cursor_that_leaves_the_catalogue_is_refused() -> None:
    """Attack: a caller hands a `cursor` that is a URL of another host: it is never fetched."""
    with opds_bench(auth="basic", base_path="/opds") as b:
        serve_komga(b, {"tome-1.epub": CLEAN})
        for cursor in (f"https://{EVIL}/x", f"//{EVIL}/x", "file:///etc/passwd", "javascript:1"):
            with pytest.raises((AcquireRefused, SourceError)):
                run(b.acquirer.search(KEY, "titre", cursor=cursor))
        assert b.wire.sent_to(EVIL) == []


# -- pagination --------------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-006", "ENF-SEC-002")
def test_opds_infinite_pagination_stops() -> None:
    """Attack: a series feed whose `next` is itself, a loop of two pages, and pages without end.
    The reading stops at a bound and says why; requests stay bounded."""
    from shelfhold.plugin_host.adapters.native.opds import MAX_PAGES

    shapes: dict[str, Any] = {
        "self": lambda path, n: path,
        "loop": lambda path, n: f"{path}?p={n % 2}",
        "endless": lambda path, n: f"{path}?p={n + 1}",
    }
    for name, nxt in shapes.items():
        with opds_bench(auth="basic", base_path="/opds") as b:
            serve_komga(b, {"tome-1.epub": CLEAN})
            counter = {"n": 0}

            def pages(
                request: httpx2.Request, nxt: Any = nxt, counter: dict[str, int] = counter
            ) -> httpx2.Response:
                counter["n"] += 1
                from tests.fakes.opds.feeds import link

                entry = acquisition_entry(
                    f"urn:test:e{counter['n']}", "Livre", f"/opds/f/{counter['n']}.epub"
                )
                target = nxt(request.url.path, counter["n"])
                body = atom_feed("urn:s", "S", [entry], links=[link("next", target, ATOM)])
                return httpx2.Response(200, content=body, headers={"content-type": ATOM})

            b.wire.serve("/opds/v1.2/series/7", pages)
            ref = SeriesRef(source_key=KEY, remote_id="/opds/v1.2/series/7", title="Un livre")
            with pytest.raises((AcquireRefused, SourceError)):
                run(b.acquirer.prepare(ref, unit="urn:test:e1"))
            assert counter["n"] <= MAX_PAGES + 1, name


@pytest.mark.req("EF-SRC-006", "ENF-SEC-002")
def test_opds_search_does_not_walk_the_pages_itself() -> None:
    """A search reads one page and hands back the cursor: the pace of the pages is the caller's."""
    with opds_bench(auth="basic", base_path="/opds") as b:
        serve_komga(b, {"tome-1.epub": CLEAN})
        from tests.fakes.opds.feeds import link

        body = atom_feed(
            "urn:s", "S", [], links=[link("next", "/opds/v1.2/series?search=x&page=2", ATOM)]
        )
        b.wire.serve("/opds/v1.2/series", document(body, ATOM))
        page = run(b.acquirer.search(KEY, "titre"))
        series_calls = [r for r in b.wire.recorder.requests if r.url.path == "/opds/v1.2/series"]
        assert len(series_calls) == 1 and page.next is not None


# -- the chain of an untrusted file ---------------------------------------------------------


@pytest.mark.req("EF-BIB-051", "EF-SRC-006")
def test_opds_epub_goes_through_the_untrusted_chain() -> None:
    """The EPUB a catalogue hands over is `untrusted`: neutralized before any export, the original
    kept apart. Nothing the feed or the settings say changes that."""
    claims = {"content_trust": "publisher", "publisher": "true"}
    with opds_bench(auth="basic", base_path="/opds", extra_settings=claims) as b:
        serve_komga(b, {"tome-1.epub": TRAPPED})
        prepared, outcome = acquire(b)
        assert outcome.state == "fetched"
        (binding,) = b.rows("SELECT content_trust FROM source_binding")
        assert binding["content_trust"] == "untrusted"
        (library,) = b.lib.files_under(b.lib.manga)
        assert b"<script" not in library.read_bytes() or _clean_of_scripts(library)
        assert b.lib.files_under(b.lib.store)  # the original, apart
        assert servable_epub(b.lib.engine, b.lib.fsops, prepared.unit_id).path == str(library)


def _clean_of_scripts(path: Any) -> bool:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        return not any(b"<script" in archive.read(n) for n in archive.namelist())


@pytest.mark.req("EF-BIB-051", "EF-SRC-006")
def test_opds_epub_announced_as_a_cbz_is_still_neutralized() -> None:
    """Attack: the feed says `application/zip` and the link is named `.cbz`: the bytes decide."""
    with opds_bench(auth="basic", base_path="/opds") as b:
        serve_komga(b, {"tome-1.cbz": TRAPPED})
        _prepared, outcome = acquire(b)
        assert outcome.state == "fetched" and outcome.kind == "epub"
        (library,) = b.lib.files_under(b.lib.manga)
        assert _clean_of_scripts(library)
        assert b.lib.files_under(b.lib.store)


@pytest.mark.req("EF-SRC-006", "ENF-SEC-015")
def test_opds_hostile_archive_is_refused_by_the_allowlist() -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("page.exe", b"MZ")
    with opds_bench(auth="basic", base_path="/opds") as b:
        serve_komga(b, {"tome-1.epub": buffer.getvalue()})
        _prepared, outcome = acquire(b)
        assert outcome.state == "abandoned"
        assert b.rows("SELECT id FROM content") == []


# -- the references ----------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-006", "EF-SRC-035")
def test_opds_references_hold_no_secret_whatever_the_feed_puts_in_a_link() -> None:
    """Attack: a feed puts the key in a query parameter, a fragment or `user:password@` of a link
    it hands back: the reference is refused or rewritten, never stored with it."""
    with opds_bench(auth="api_key_header", base_path="/opds") as b:
        serve_komga(b, {"tome-1.epub": CLEAN})
        key = b.secrets["api_key"]
        for href in (
            f"/opds/v1.2/series/7?apikey={key}",
            f"/opds/v1.2/series/7#{key}",
            f"https://user:{key}@{HOST}/opds/v1.2/series/7",
        ):
            from tests.fakes.opds import navigation_entry

            body = atom_feed("urn:f", "F", [navigation_entry("urn:test:s", "Un livre", href)])
            b.wire.serve("/opds/v1.2/series", document(body, ATOM))
            try:
                found = search(b)
            except Exception:
                found = []
            assert all(key not in r.remote_id for r in found)
        assert key not in b.database_text()


@pytest.mark.req("EF-SRC-006")
def test_opds_manifest_is_untrusted_and_bound_to_the_configured_host() -> None:
    from pathlib import Path

    from shelfhold.plugin_host.native_sources import opds_source
    from shelfhold.sdk.source import Capability, SourceManifest

    source = opds_source()
    on_disk = SourceManifest.model_validate_json(
        (Path(__file__).resolve().parents[3] / "sources" / "opds" / "manifest.json").read_text(
            "utf-8"
        )
    )
    assert source.manifest == on_disk
    assert on_disk.content_trust == "untrusted" and on_disk.key == "opds"
    assert Capability.SEARCH in on_disk.capabilities
    (rule,) = on_disk.permissions.domains
    assert rule.base_url_setting == "base_url"  # the host is the one the admin configured
    kinds = {(a.kind, getattr(a, "location", None)) for a in on_disk.auth}
    assert {
        ("basic", None),
        ("api_key", "header"),
        ("api_key", "query"),
        ("api_key", "path"),
    } <= kinds


# -- OPDS 2.0, errors, configuration --------------------------------------------------------------


@pytest.mark.req("EF-SRC-006")
def test_opds_2_0_catalogue_search_and_acquire() -> None:
    """The JSON flavour end to end: a templated search link, publications, a file."""
    from tests.fakes.opds import OPDS2, opds2_feed, publication

    with opds_bench(auth="none", base_path="/opds/v2") as b:
        root = opds2_feed(
            "Catalogue",
            links=[
                {
                    "rel": "search",
                    "href": "/opds/v2/search{?query}",
                    "type": OPDS2,
                    "templated": True,
                }
            ],
        )
        found = opds2_feed(
            "Résultats",
            publications=[publication("pub-1", "Un livre 2.0", "/opds/v2/files/pub-1.epub")],
        )
        b.wire.serve("/opds/v2", document(root, OPDS2))
        b.wire.serve("/opds/v2/search", document(found, OPDS2))
        b.wire.serve("/opds/v2/files/pub-1.epub", file(CLEAN))
        (ref,) = search(b, "un titre")
        assert ref.title == "Un livre 2.0" and ref.memo == {"entry": "pub-1"}
        queries = [r.url.params.get("query") for r in b.wire.recorder.requests]
        assert "un titre" in queries
        _prepared, outcome = acquire(b, ref)
        assert outcome.state == "fetched"


@pytest.mark.req("EF-SRC-006")
def test_opds_http_errors_are_typed_source_errors() -> None:
    from shelfhold.sdk.source import (
        AuthRequired,
        ContentRemoved,
        RateLimited,
        SourceChanged,
        TemporarilyUnavailable,
    )

    cases: dict[int, type[Exception]] = {
        401: AuthRequired,
        403: AuthRequired,
        404: ContentRemoved,
        429: RateLimited,
        503: TemporarilyUnavailable,
        418: SourceChanged,
    }
    for status, expected in cases.items():
        with opds_bench(auth="none", base_path="/opds") as b:
            headers = {"retry-after": "7"} if status == 429 else {}
            b.wire.serve("/opds", lambda _r, s=status, h=headers: httpx2.Response(s, headers=h))
            with pytest.raises(expected) as caught:
                search(b)
            if status == 429:
                assert caught.value.retry_after == 7.0  # type: ignore[attr-defined]


@pytest.mark.req("EF-SRC-006")
def test_opds_html_or_garbage_instead_of_a_feed_is_a_source_that_changed() -> None:
    from shelfhold.sdk.source import SourceChanged

    for body in (b"<html><body>login</body></html>", b"\x00\xff\x00", b"{}", b""):
        with opds_bench(auth="none", base_path="/opds") as b:
            b.wire.serve("/opds", document(body, "text/html"))
            with pytest.raises(SourceChanged):
                search(b)


@pytest.mark.req("EF-SRC-006", "EF-SRC-023")
def test_opds_address_that_is_not_a_catalogue_is_refused_before_any_request() -> None:
    for bad in ("", "ftp://opds.example.org/x", "opds.example.org", "https://", "http://h:99999/x",
                f"https://opds.example.org/x#{new_key()}"):  # fmt: skip
        with opds_bench(auth="none", base_path="/opds") as b:
            b.settings["base_url"] = bad
            with pytest.raises((AcquireRefused, SourceError)):
                search(b)
            assert b.wire.recorder.requests == []


@pytest.mark.req("EF-SRC-023")
def test_opds_credentials_chosen_but_not_set_send_nothing() -> None:
    """`auth` says basic and no password is set: no request goes out unauthenticated."""
    for mode, secret_name in (("basic", "password"), ("api_key_header", "api_key")):
        with opds_bench(auth=mode, base_path="/opds") as b:
            serve_komga(b, {"tome-1.epub": CLEAN})
            b.secrets.pop(secret_name)
            from shelfhold.sdk.source import AuthRequired

            with pytest.raises(AuthRequired):
                search(b)
            assert b.wire.recorder.requests == []
        with opds_bench(auth="api_key_nowhere", base_path="/opds") as b:  # not a declared flow
            with pytest.raises(SourceError):
                search(b)
            assert b.wire.recorder.requests == []


@pytest.mark.req("EF-SRC-006")
def test_opds_a_source_that_stopped_being_configured_closes_the_request_for_good() -> None:
    with opds_bench(auth="basic", base_path="/opds") as b:
        serve_komga(b, {"tome-1.epub": CLEAN})
        (ref,) = search(b)
        prepared = run(b.acquirer.prepare(ref))
        b.settings["base_url"] = ""  # the admin cleared it between the request and the job
        outcome = run(b.acquirer.fetch(prepared.request_id, b.store(), b.place))
        assert outcome.state == "abandoned" and outcome.reason == "source.not_configured"
