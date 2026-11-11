"""Regression tests of the review of #143 (M2 L19): the adversarial cases that were proved by hand.

Trapped EPUB variants never reach /library raw, odd hosts and URLs never reach another host, a
redirect to a private address is refused, a source cannot replace the User-Agent, and an answer made
for a cookie session is never served from the cache to an anonymous request."""

from __future__ import annotations

import asyncio
import io
import zipfile
from typing import Any

import httpx2
import pytest

from shelfhold.infrastructure.fetcher.guard import DomainAllowlist
from shelfhold.library.public import sniff_kind
from shelfhold.sdk.models import DomainRule
from shelfhold.sdk.testing.corpus.trapped_epubs.build import epub_bytes, xhtml
from tests.acquire_env import EVIL, HOST, NativeBooks, acquire_bench
from tests.library_env import cbz_bytes

TRAPPED = epub_bytes(xhtml("<p>x</p><script>alert(1)</script>"))
FILE = "/files/book-42.epub"


def run(c: Any) -> Any:
    return asyncio.run(c)


def _assert_not_raw(b, outcome):
    lib = b.lib.files_under(b.lib.manga)
    for f in lib:
        assert b"<script" not in f.read_bytes(), f"raw active content exported: {f}"


def _deflated_mimetype_epub() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_DEFLATED
        )
        with zipfile.ZipFile(io.BytesIO(TRAPPED)) as src:
            for n in src.namelist():
                if n != "mimetype":
                    z.writestr(n, src.read(n))
    return buf.getvalue()


def _missing_mimetype() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(TRAPPED)) as src, zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            if n != "mimetype":
                z.writestr(n, src.read(n))
    return buf.getvalue()


def _epub_in_cbz() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(cbz_bytes())) as src, zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            z.writestr(n, src.read(n))
        z.writestr("inner.epub", TRAPPED)
    return buf.getvalue()


def _cbz_with_hidden_epub_tail() -> bytes:
    # a valid CBZ with a trapped EPUB appended after the EOCD (polyglot tail)
    return cbz_bytes() + TRAPPED


def _epub_prefixed_by_cbz() -> bytes:
    return cbz_bytes() + TRAPPED  # same; readers that seek EOCD read the EPUB


def _png_named_xhtml_in_cbz() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(cbz_bytes())) as src, zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            z.writestr(n, src.read(n))
        z.writestr("p.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>")
    return buf.getvalue()


CASES = {
    "deflated_mimetype": _deflated_mimetype_epub,
    "missing_mimetype": _missing_mimetype,
    "epub_in_cbz": _epub_in_cbz,
    "polyglot_tail": _cbz_with_hidden_epub_tail,
    "svg_in_cbz": _png_named_xhtml_in_cbz,
}

pytestmark = pytest.mark.req("EF-SRC-015", "EF-BIB-051", "ENF-LEG-007")


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("mime", ["application/epub+zip", "application/x-cbz"])
def test_trapped_variants_never_reach_library_raw(case: str, mime: str) -> None:
    data = CASES[case]()
    with acquire_bench(NativeBooks(trust="publisher", mime=mime), files={FILE: data}) as b:
        _p, outcome = b.prepared_and_fetched()
        _assert_not_raw(b, outcome)


def test_sniff_kind_epub_with_big_extra_field_reads_as_cbz(tmp_path) -> None:
    raw = bytearray(TRAPPED)  # patch first local header: claim extra_len 5000 -> start beyond 4 KiB
    p = tmp_path / "x"
    p.write_bytes(bytes(raw))
    assert sniff_kind(p) == ".epub"


@pytest.mark.parametrize(
    "host,expected",
    [
        (HOST, True),
        (HOST.upper(), True),
        (HOST + ".", True),
        ("xn--bks-1na.example.org", False),
        ("böoks.example.org", False),
        (f"{EVIL}", False),
        (f"x.{HOST}", False),
    ],
)
def test_allowlist_edge_hosts(host: str, expected: bool) -> None:
    allow = DomainAllowlist("k", [DomainRule(pattern=HOST, purpose="api")])
    assert allow.allows(host) is expected, host


@pytest.mark.parametrize(
    "url",
    [
        f"https://{EVIL}\\@{HOST}/book/42",
        f"https://{HOST}./book/42",
        f"https://{HOST}:443@{EVIL}/book/42",
    ],
)
def test_resolve_odd_urls_do_not_reach_evil(url: str) -> None:
    with acquire_bench() as b:
        try:
            run(b.acquirer.resolve(url))
        except Exception as e:
            assert e is not None
        assert not any(p.startswith(EVIL) for p in b.wire.paths)


def test_redirect_to_private_ip_on_allowed_host_refused() -> None:
    with acquire_bench(files={FILE: TRAPPED}) as b:
        orig = b.wire.respond

        def red(r):
            if r.url.path == FILE:
                return httpx2.Response(302, headers={"location": "http://127.0.0.1/x.epub"})
            return orig(r)

        b.wire.respond = red  # type: ignore[method-assign]
        _p, outcome = b.prepared_and_fetched()
        assert outcome.state == "abandoned"
        assert not any("127.0.0.1" in p for p in b.wire.paths)


def test_fileasset_headers_can_override_user_agent() -> None:
    class Spoof(NativeBooks):
        async def fetch_unit(self, f, ref, u):
            a = await super().fetch_unit(f, ref, u)
            return a.model_copy(
                update={
                    "request": a.request.model_copy(
                        update={"headers": {"User-Agent": "Mozilla/5.0 spoof"}}
                    )
                }
            )

    with acquire_bench(Spoof(trust="publisher"), files={FILE: TRAPPED}) as b:
        b.prepared_and_fetched()
        uas = [r.headers.get("user-agent") for r in b.wire.recorder.requests if r.url.path == FILE]
        assert uas
        assert all("spoof" not in (ua or "") for ua in uas), uas


def test_cookie_session_response_is_cached_and_served_to_the_next_anonymous_search() -> None:
    """httpx keeps Set-Cookie in the client jar; the next request carries Cookie without the
    caller's headers saying so, so `cacheable_request` sees an anonymous GET and files it."""

    class Session(NativeBooks):
        async def search(self, f, q, filters, cursor, *, sub=None):
            if not getattr(self, "logged", False):
                self.logged = True
                await f.get(f"https://{HOST}/login")
            return await super().search(f, q, filters, cursor, sub=sub)

    with acquire_bench(Session()) as b:
        orig = b.wire.respond

        def resp(r):
            if r.url.path == "/login":
                return httpx2.Response(200, headers={"set-cookie": "sid=SECRET; Path=/"})
            if r.url.path == "/api/search":
                who = "member" if "sid=SECRET" in r.headers.get("cookie", "") else "anon"
                return httpx2.Response(
                    200,
                    json={"items": [{"id": who, "title": who}]},
                    headers={"cache-control": "max-age=600"},
                )
            return orig(r)

        b.wire.respond = resp  # type: ignore[method-assign]
        first = run(b.acquirer.search("native-books", "login"))
        cookies = [
            r.headers.get("cookie") for r in b.wire.recorder.requests if r.url.path == "/api/search"
        ]
        assert not any(cookies), cookies  # the jar keeps nothing
        assert first.items
        # a later operation, with no session: same URL, so the cache is what could leak
        second = run(b.acquirer.search("native-books", "login"))  # new operation, no login
        hits = [r for r in b.wire.recorder.requests if r.url.path == "/api/search"]
        assert hits
        assert second.items[0].remote_id == "anon", (
            "session answer served from the cache to an anonymous operation"
        )
