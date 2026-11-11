"""M2 L19 (EF-SRC-015, EF-SRC-004, EF-BIB-051, ENF-LEG-007): a book asked for once from a native
source, SeriesRef -> FileAsset -> staging -> ingestion, over the real Fetcher, registry, catalog and
stores. Each "never" of the lot is a test that tries to break it."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from pathlib import Path
from typing import Any

import pytest

from shelfhold.acquisition.public import AcquireRefused
from shelfhold.infrastructure.fetcher import DomainNotAllowed
from shelfhold.library.public import servable_epub
from shelfhold.plugin_host.errors import NoSourceForUrl, RefHasSecret
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member, epub_bytes, xhtml, zip_bytes
from tests.acquire_env import BOOK, EVIL, HOST, KEY, PAGE, NativeBooks, acquire_bench
from tests.library_env import cbz_bytes

TRAPPED = epub_bytes(xhtml("<p>chapitre</p><script>alert(1)</script>"))
CLEAN = epub_bytes(xhtml("<p>chapitre</p>"))
FILE = "/files/book-42.epub"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def has_active_content(path: Path) -> bool:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        return any(b"<script" in archive.read(n) for n in archive.namelist())


# -- the flow, and what it keeps ------------------------------------------------------------------


@pytest.mark.req("EF-SRC-015")
def test_passthrough_keeps_received_sha256() -> None:
    """A publisher's EPUB is filed as received: the hash the staging counted is the hash kept."""
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "fetched" and outcome.kind == "epub"
        assert outcome.sha256 == sha(CLEAN)
        (content,) = b.rows("SELECT original_sha256, format FROM content")
        assert content["original_sha256"] == sha(CLEAN) and content["format"] == "epub"
        (file,) = b.lib.files_under(b.lib.manga)
        assert file.read_bytes() == CLEAN  # the bytes of the publisher, not rewritten
        assert prepared.created


@pytest.mark.req("EF-SRC-015", "EF-BIB-001")
def test_novel_work_is_filed_with_a_demande_for_its_requester() -> None:
    """The Work is a `novel`; the request is a persistent `download_request` that says who asked
    and who may see it; the file is "rangé" with one event, and the request is fetched."""
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        prepared, _done = b.prepared_and_fetched(
            requester_user_id="01JFZ0000000000000000000AB", visibility="private"
        )
        (work,) = b.rows("SELECT kind, title FROM work")
        assert work["kind"] == "novel" and work["title"] == "Un livre de test"
        (edition,) = b.rows("SELECT media FROM edition")
        assert edition["media"] == "text"
        (request,) = b.rows(
            "SELECT state, visibility, requester_user_id, target_kind FROM download_request"
        )
        assert request == {
            "state": "fetched", "visibility": "private",
            "requester_user_id": "01JFZ0000000000000000000AB", "target_kind": "unit",
        }  # fmt: skip
        (stored,) = b.delivered
        assert stored.type == "unit.stored" and stored.payload["kind"] == "epub"
        assert stored.payload["unit_id"] == prepared.unit_id
        (event,) = b.rows("SELECT visibility, actor_id FROM event WHERE type = 'unit.stored'")
        assert event["visibility"] == "private"
        # a replay finds the Unit filed: nothing is asked again
        again = run(b.acquirer.prepare(b.plugin._series()))
        assert again.request_id is None and again.unit_id == prepared.unit_id
        assert len(b.rows("SELECT id FROM work")) == 1


@pytest.mark.req("EF-SRC-015", "EF-BIB-051")
def test_acquire_copies_manifest_trust_to_binding() -> None:
    for declared in ("publisher", "untrusted"):
        with acquire_bench(NativeBooks(trust=declared), files={FILE: CLEAN}) as b:
            b.prepared_and_fetched()
            (binding,) = b.rows("SELECT content_trust FROM source_binding")
            assert binding["content_trust"] == declared


@pytest.mark.req("EF-BIB-051", "EF-SRC-015")
def test_publisher_declared_by_a_plugin_the_host_does_not_know_is_not_taken_on_its_word() -> None:
    """The manifest says `publisher`, the plugin is no plugin of the host and nobody attested it:
    the binding keeps what the manifest said, and the neutralizer runs all the same."""
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: TRAPPED}, known=False) as b:
        prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "fetched"
        (library,) = b.lib.files_under(b.lib.manga)
        assert not has_active_content(library) and b.lib.files_under(b.lib.store)
        assert Path(servable_epub(b.lib.engine, b.lib.fsops, prepared.unit_id).path) == library


@pytest.mark.req("EF-SRC-004", "EF-SRC-015")
def test_acquire_by_url_uses_resolved_source() -> None:
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        ref = run(b.acquirer.resolve(PAGE))
        assert (ref.source_key, ref.remote_id) == (KEY, BOOK)
        assert b.plugin.calls == [f"resolve:{PAGE}"]
        _prepared, outcome = b.prepared_and_fetched(ref)
        assert outcome.state == "fetched"
        (item,) = b.rows("SELECT plugin_id, source_key FROM source_item")
        assert item["plugin_id"] == KEY


@pytest.mark.req("EF-SRC-004")
def test_unrecognised_url_is_no_source_and_sends_nothing() -> None:
    with acquire_bench() as b:
        with pytest.raises(NoSourceForUrl):
            run(b.acquirer.resolve("https://elsewhere.example/book/1"))
        assert b.wire.recorder.requests == [] and b.plugin.calls == []


@pytest.mark.req("EF-SRC-015")
def test_search_goes_through_the_source_fetcher_and_validates_filters() -> None:
    with acquire_bench() as b:
        page = run(b.acquirer.search(KEY, "livre", filters={"language": "en"}))
        assert [r.remote_id for r in page.items] == [BOOK]
        assert [f.key for f in b.acquirer.filters(KEY)] == ["language"]
        with pytest.raises(AcquireRefused) as unknown:
            run(b.acquirer.search(KEY, "livre", filters={"nope": "x"}))
        assert unknown.value.code == "source.filter_invalid"
        with pytest.raises(AcquireRefused) as none:
            run(b.acquirer.search("no-such-source", "x"))
        assert none.value.code == "plugin.unknown"


# -- EF-BIB-051: an untrusted EPUB is never exported raw ----------------------------------------


@pytest.mark.req("EF-BIB-051", "EF-SRC-015")
def test_untrusted_epub_received_by_acquisition_is_never_exported_raw() -> None:
    """The end-to-end proof the reachability analysis cannot give: a trapped EPUB that arrives by
    acquisition takes the neutralizer, the original goes to /store, and what an export may read
    (`servable_epub`) and what /library holds are the derivative, never the bytes received."""
    with acquire_bench(NativeBooks(trust="untrusted"), files={FILE: TRAPPED}) as b:
        prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "fetched" and outcome.kind == "epub"
        (library,) = b.lib.files_under(b.lib.manga)
        (kept,) = b.lib.files_under(b.lib.store)
        assert kept.read_bytes() == TRAPPED  # the original, in /store only
        assert kept.relative_to(b.lib.store).parts[0] == "untrusted"
        assert library.read_bytes() != TRAPPED and not has_active_content(library)
        served = servable_epub(b.lib.engine, b.lib.fsops, prepared.unit_id)
        assert Path(served.path) == library  # what an export reads is the derivative
        (content,) = b.rows("SELECT original_sha256, derived_by, derived_hash FROM content")
        assert content["original_sha256"] == sha(TRAPPED) and content["derived_by"]
        assert content["derived_hash"] != content["original_sha256"]


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize("name", ["labelled.cbz", "labelled.pdf", "labelled"])
def test_epub_labelled_as_something_else_is_still_oriented_by_its_bytes(name: str) -> None:
    """The file name and the MIME type are the source's word: an EPUB that calls itself a CBZ
    reaches the neutralizer all the same."""
    with acquire_bench(
        NativeBooks(trust="untrusted", mime="application/x-cbz", file_url=f"https://{HOST}/files/{name}"),
        files={f"/files/{name}": TRAPPED},
    ) as b:  # fmt: skip
        _prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "fetched" and outcome.kind == "epub"
        (library,) = b.lib.files_under(b.lib.manga)
        assert library.suffix == ".epub" and not has_active_content(library)
        assert b.lib.files_under(b.lib.store)  # the original is kept


@pytest.mark.req("EF-BIB-051")
def test_a_file_claiming_to_be_epub_without_the_signature_is_audited_as_cbz() -> None:
    """No stored `mimetype` first: it is not an EPUB, so it meets the CBZ allowlist, which refuses
    markup. Nothing reaches /library, the request is closed, the reason is a code."""
    fake = zip_bytes(
        [Member("OEBPS/c1.xhtml", b"<script>1</script>", deflate=False),
         Member("mimetype", b"application/epub+zip", deflate=False)]
    )  # fmt: skip
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: fake}) as b:
        _prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "abandoned" and outcome.reason == "content.untrusted_entry"
        assert b.lib.files_under(b.lib.manga) == [] and b.rows("SELECT id FROM content") == []


@pytest.mark.req("EF-SRC-015")
def test_a_cbz_is_passed_through_as_received() -> None:
    data = cbz_bytes()
    with acquire_bench(
        NativeBooks(trust="publisher", mime="application/vnd.comicbook+zip"),
        files={FILE: data},
    ) as b:
        _prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "fetched" and outcome.kind == "cbz"
        (file,) = b.lib.files_under(b.lib.manga)
        assert file.suffix == ".cbz" and file.read_bytes() == data


@pytest.mark.req("EF-SRC-015")
@pytest.mark.parametrize("mime", ["application/pdf", "audio/mpeg", "text/html"])
def test_a_file_type_that_is_not_taken_is_refused_before_a_byte_is_fetched(mime: str) -> None:
    with acquire_bench(NativeBooks(mime=mime), files={FILE: b"%PDF-1.7"}) as b:
        _prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "abandoned" and outcome.reason == "acquire.unsupported_file"
        assert FILE not in " ".join(b.wire.paths) and b.lib.files_under(b.lib.manga) == []


# -- EF-SRC-003: a URL outside the DomainRules of the plugin -------------------------------------


@pytest.mark.req("EF-SRC-003", "EF-SRC-004")
def test_resolved_url_outside_the_plugin_domain_rules_is_refused_before_the_plugin() -> None:
    """The plugin declares a URL form for a host that its DomainRules do not hold: the pasted
    URL is refused by name, the plugin is not called and nothing leaves."""
    plugin = NativeBooks(patterns=(f"https://{EVIL}/book/*", f"https://{HOST}/book/*"))
    with acquire_bench(plugin) as b:
        with pytest.raises(DomainNotAllowed):
            run(b.acquirer.resolve(f"https://{EVIL}/book/42"))
        assert b.plugin.calls == [] and b.wire.recorder.requests == []


@pytest.mark.req("EF-SRC-003", "EF-SRC-015")
def test_file_url_outside_the_plugin_domain_rules_is_never_fetched() -> None:
    """The source hands over a FileAsset on another host: refused at the Fetcher, by name, before
    any resolution. The request is closed and nothing is stored."""
    evil = f"https://{EVIL}/files/book-42.epub"
    with acquire_bench(NativeBooks(file_url=evil), files={FILE: CLEAN}) as b:
        _prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "abandoned" and outcome.reason == "source.domain_not_allowed"
        assert not any(p.startswith(EVIL) for p in b.wire.paths)
        assert b.lib.files_under(b.lib.manga) == [] and b.rows("SELECT id FROM content") == []


@pytest.mark.req("EF-SRC-003")
def test_redirect_to_another_host_is_refused_at_every_hop() -> None:
    plugin = NativeBooks()
    with acquire_bench(plugin, files={FILE: CLEAN}) as b:
        b.wire.files.pop(FILE)
        original = b.wire.respond

        def redirecting(request: Any) -> Any:
            import httpx2

            if request.url.path == FILE:
                return httpx2.Response(302, headers={"location": f"https://{EVIL}/x.epub"})
            return original(request)

        b.wire.respond = redirecting  # type: ignore[method-assign]
        _prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "abandoned" and outcome.reason == "source.domain_not_allowed"
        assert not any(p.startswith(EVIL) for p in b.wire.paths)


# -- ENF-LEG-007: robots.txt and the cache -------------------------------------------------------


@pytest.mark.req("ENF-LEG-007", "EF-SRC-015")
def test_acquisition_never_ignores_robots() -> None:
    robots = "User-agent: *\nDisallow: /files/\n"
    with acquire_bench(NativeBooks(), files={FILE: CLEAN}, robots=robots) as b:
        _prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "abandoned" and outcome.reason == "fetch.robots_disallowed"
        assert f"{HOST}/robots.txt" in b.wire.paths  # it was read, and obeyed
        assert f"{HOST}{FILE}" not in b.wire.paths
        assert b.lib.files_under(b.lib.manga) == []


@pytest.mark.req("ENF-LEG-007")
def test_robots_that_cannot_be_read_keeps_the_request_open_until_robots_is_back() -> None:
    """A robots.txt that answers a server error is an outage: nothing is fetched, the request is
    kept and asked again (never "nothing restricted", never a final refusal)."""
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        original = b.wire.respond
        down = {"on": True}

        def broken(request: Any) -> Any:
            import httpx2

            if down["on"] and request.url.path == "/robots.txt":
                return httpx2.Response(503)
            return original(request)

        b.wire.respond = broken  # type: ignore[method-assign]
        prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "waiting" and outcome.reason == "RobotsUnavailable"
        assert f"{HOST}{FILE}" not in b.wire.paths
        assert b.rows("SELECT state FROM download_request") == [{"state": "requested"}]
        down["on"] = False  # robots is back, and the short retry delay has passed
        for policy in b.fetchers._robots.values():
            policy.clock = lambda: time.time() + 3600
        assert b.rows("SELECT state FROM download_request") == [{"state": "requested"}]
        again = run(b.acquirer.fetch(prepared.request_id, b.store(), b.place))
        assert again.state == "fetched"


@pytest.mark.req("ENF-LEG-007")
def test_search_reads_robots_too() -> None:
    from shelfhold.infrastructure.fetcher import RobotsDisallowed

    with acquire_bench(NativeBooks(), robots="User-agent: *\nDisallow: /api/\n") as b:
        with pytest.raises(RobotsDisallowed):
            run(b.acquirer.search(KEY, "livre"))
        assert f"{HOST}/api/search" not in b.wire.paths


@pytest.mark.req("ENF-LEG-007")
def test_the_search_is_cached_per_source_and_an_authenticated_request_never_is() -> None:
    with acquire_bench(NativeBooks()) as b:
        run(b.acquirer.search(KEY, "livre"))
        run(b.acquirer.search(KEY, "livre"))
        assert b.wire.paths.count(f"{HOST}/api/search") == 1  # the second is the cache's
        record = b.registry.get(KEY)

        async def get(headers: dict[str, str] | None) -> None:
            fetcher = b.fetchers.for_record(record)
            try:
                await fetcher.get(f"https://{HOST}/api/search", headers=headers)
            finally:
                await fetcher.aclose()

        sent = b.wire.paths.count(f"{HOST}/api/search")
        for _ in range(2):  # a credential: never read from the cache, never stored in it
            run(get({"Authorization": "Bearer " + secrets.token_hex(8)}))
        assert b.wire.paths.count(f"{HOST}/api/search") == sent + 2
        for _ in range(2):
            run(get({"Cookie": "session=" + secrets.token_hex(8)}))
        assert b.wire.paths.count(f"{HOST}/api/search") == sent + 4


@pytest.mark.req("ENF-LEG-007")
def test_a_credentialed_response_is_not_served_to_the_next_anonymous_request() -> None:
    """The first request carries a credential and gets a cacheable answer: it is not stored, so
    the anonymous one that follows goes to the wire (an answer made for an account is never
    shown to another)."""
    with acquire_bench(NativeBooks()) as b:
        record = b.registry.get(KEY)

        async def get(headers: dict[str, str] | None) -> None:
            fetcher = b.fetchers.for_record(record)
            try:
                await fetcher.get(f"https://{HOST}/api/search", headers=headers)
            finally:
                await fetcher.aclose()

        run(get({"Authorization": "Bearer " + secrets.token_hex(8)}))
        run(get(None))
        assert b.wire.paths.count(f"{HOST}/api/search") == 2


@pytest.mark.req("ENF-LEG-007")
def test_the_cache_is_off_when_the_config_says_so() -> None:
    with acquire_bench(NativeBooks(), cache_enabled=False) as b:
        run(b.acquirer.search(KEY, "livre"))
        run(b.acquirer.search(KEY, "livre"))
        assert b.wire.paths.count(f"{HOST}/api/search") == 2


# -- EF-SRC-035: a secret in a reference ---------------------------------------------------------


@pytest.mark.req("EF-SRC-035", "EF-SRC-015")
def test_a_reference_holding_a_secret_is_refused_and_nothing_is_kept() -> None:
    leaky = f"{BOOK}?api_key={secrets.token_hex(12)}"
    with acquire_bench(NativeBooks(remote_id=leaky), files={FILE: CLEAN}) as b:
        with pytest.raises(RefHasSecret):
            run(b.acquirer.resolve(PAGE))  # the plugin hands back a reference with a key in it
        with pytest.raises(RefHasSecret):
            run(b.acquirer.prepare(SeriesRef(source_key=KEY, remote_id=leaky, title="x")))
        counted = b.rows(
            "SELECT (SELECT count(*) FROM work) AS w, (SELECT count(*) FROM source_item) AS i,"
            " (SELECT count(*) FROM source_binding) AS b,"
            " (SELECT count(*) FROM download_request) AS r"
        )
        assert counted == [{"w": 0, "i": 0, "b": 0, "r": 0}]


@pytest.mark.req("EF-SRC-035")
def test_a_pasted_url_with_userinfo_is_not_a_source() -> None:
    with acquire_bench() as b:
        with pytest.raises(NoSourceForUrl):
            run(b.acquirer.resolve(f"https://user:pw@{HOST}/book/42"))
        assert b.plugin.calls == []


# -- the hash of the file ------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-015", "ENF-SEC-002")
def test_a_file_that_is_not_what_was_counted_is_refused() -> None:
    """The bytes change between the staging (which hashed them) and the filing: the store checks
    the hash it was given against the file, refuses, and keeps nothing."""

    def tamper(staged: Path) -> None:
        staged.write_bytes(staged.read_bytes() + b"\0tampered")

    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:

        async def go() -> Any:
            prepared = await b.acquirer.prepare(b.plugin._series())
            assert prepared.request_id is not None
            return await b.acquirer.fetch(prepared.request_id, b.store(before=tamper), b.place)

        outcome = run(go())
        assert outcome.state == "abandoned" and outcome.reason == "received_hash_mismatch"
        assert b.rows("SELECT id FROM content") == [] and b.lib.files_under(b.lib.manga) == []
        assert b.delivered == []  # nothing was "rangé"


@pytest.mark.req("ENF-SEC-002")
def test_a_file_over_the_bound_is_refused_and_leaves_nothing() -> None:
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        b.acquirer._max = 64  # the bound the composition gives the receiver
        _prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "abandoned" and outcome.reason == "fetch.too_large"
        assert b.lib.files_under(b.lib.manga) == [] and b.rows("SELECT id FROM content") == []


# -- the series and the units --------------------------------------------------------------------


@pytest.mark.req("EF-SRC-015", "EF-BIB-053")
def test_a_series_deleted_on_purpose_is_not_recreated() -> None:
    with acquire_bench() as b:
        with pytest.raises(AcquireRefused) as refused:
            run(b.acquirer.prepare(b.plugin._series(), tombstoned=lambda _binding: True))
        assert refused.value.code == "acquire.series_tombstoned"
        assert b.rows("SELECT count(*) AS n FROM work")[0]["n"] == 0


@pytest.mark.req("EF-SRC-015")
def test_a_source_that_is_not_active_or_not_a_source_is_unknown() -> None:
    with acquire_bench() as b:
        with pytest.raises(AcquireRefused) as unknown:
            run(b.acquirer.prepare(SeriesRef(source_key="other", remote_id="x", title="x")))
        assert unknown.value.code == "plugin.unknown"


@pytest.mark.req("EF-SRC-015")
def test_a_request_that_waits_is_asked_again_and_a_replay_does_not_file_twice() -> None:
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        spare = dict(b.wire.files)
        b.wire.files.clear()  # the server does not have the file now
        prepared, outcome = b.prepared_and_fetched()
        assert outcome.state == "waiting"  # asked again later, not a refusal, nothing filed
        assert b.rows("SELECT state, attempts FROM download_request") == [
            {"state": "requested", "attempts": 1}
        ]
        b.wire.files.update(spare)  # the file is back: the same request goes through
        first = run(b.acquirer.fetch(prepared.request_id, b.store(), b.place))
        replay = run(b.acquirer.fetch(prepared.request_id, b.store(), b.place))
        assert first.state == "fetched" and replay.reason == "nothing_to_do"
        assert len(b.rows("SELECT id FROM content")) == 1


@pytest.mark.req("EF-SRC-015")
def test_a_refused_request_leaves_no_stored_event() -> None:
    with acquire_bench(NativeBooks(mime="application/pdf")) as b:
        b.prepared_and_fetched()
        assert b.rows("SELECT count(*) AS n FROM event WHERE type = 'unit.stored'")[0]["n"] == 0
