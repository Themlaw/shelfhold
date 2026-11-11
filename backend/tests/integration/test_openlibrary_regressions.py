# ruff: noqa: F811  (the `bench` fixture is imported from the enrichment tests)
"""L13 regressions from the cross-review of PR #145: the quota is one balance in the database
(coroutines, Fetchers and processes share it, the day's cap included), the cache is the host's
and outlives the process, a cover never follows a redirect off the declared domains, and the
plugin cannot set the user agent by any spelling of the header. No network."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher.errors import ProviderQuotaExceeded
from shelfhold.infrastructure.fetcher.limiter import ProviderBuckets
from shelfhold.infrastructure.fetcher.quota_store import SqlBucketStore
from shelfhold.infrastructure.schema import metadata
from shelfhold.plugin_host.provider_fetcher import ProviderRuntime
from shelfhold.sdk.metadata import ProviderQuota
from tests.fakes.openlibrary.server import COVER_HOST, ISBN
from tests.integration.test_openlibrary_enrich import Bench, FakeOpenLibrary, bench  # noqa: F401

Q = ProviderQuota(requests_per_second=1.0, burst=1, daily_cap=3)


class T:
    def __init__(self):
        self.t = 1_000.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)  # no clock advance: concurrent at one instant


def _engine(tmp: Path):
    e = create_db_engine(tmp / "q.db")
    metadata.create_all(e)
    return e


@pytest.mark.req("ENF-LEG-005", "ENF-LEG-007")
def test_each_enrich_call_gets_a_fresh_bucket_so_concurrency_doubles_the_rate(tmp_path) -> None:
    """ops/meta.py:_enrich calls use_store(SqlBucketStore(engine)) every time: a new store object
    clears the buckets, so two calls in flight hold two independent buckets."""
    e, t = _engine(tmp_path), T()
    b = ProviderBuckets(t.now, t.sleep, t.now)
    b.use_store(SqlBucketStore(e))
    first = b.bucket_for("openlibrary", Q)
    b.use_store(SqlBucketStore(e))
    second = b.bucket_for("openlibrary", Q)

    async def go():
        return await asyncio.gather(
            *(x.acquire() for x in (first, second, first, second)), return_exceptions=True
        )

    results = asyncio.run(go())
    assert sum(isinstance(r, ProviderQuotaExceeded) for r in results) == 1  # the 4th of cap 3
    assert sorted(t.slept) == [1.0, 2.0]  # one balance: spaced, never at the same instant
    assert first is second, "two buckets for one provider in one process"


@pytest.mark.req("ENF-LEG-005", "ENF-LEG-007")
def test_daily_cap_resets_on_every_enrich_call(tmp_path) -> None:
    e, t = _engine(tmp_path), T()
    b = ProviderBuckets(t.now, t.sleep, t.now)
    taken = 0
    for _ in range(10):  # 10 sequential `meta enrich` calls through ops/meta.py
        b.use_store(SqlBucketStore(e))
        try:
            asyncio.run(b.bucket_for("openlibrary", Q).acquire())
            taken += 1
        except ProviderQuotaExceeded:
            break
    print("requests allowed under daily_cap=3:", taken)
    assert taken <= 3


@pytest.mark.req("ENF-LEG-005", "ENF-LEG-007")
def test_two_processes_share_no_bucket(tmp_path) -> None:
    e = _engine(tmp_path)
    ta, tb = T(), T()
    api = ProviderBuckets(ta.now, ta.sleep, ta.now, store=SqlBucketStore(e))
    worker = ProviderBuckets(tb.now, tb.sleep, tb.now, store=SqlBucketStore(e))
    a, w = (
        api.bucket_for("openlibrary", Q.model_copy(update={"daily_cap": None})),
        worker.bucket_for("openlibrary", Q.model_copy(update={"daily_cap": None})),
    )

    async def go(x):
        for _ in range(5):
            await x.acquire()

    asyncio.run(go(a))
    asyncio.run(go(w))
    print("api waits:", ta.slept, "worker waits:", tb.slept)
    assert tb.slept and tb.slept[0] >= 4, "the worker did not see the API's debt: 2x the rate"


@pytest.mark.req("ENF-LEG-005", "ENF-LEG-007")
def test_mandatory_cache_without_upstream_freshness_headers(bench) -> None:
    bench.fake.cache_control = None
    bench.enrich()
    bench.enrich()
    print("api calls, no Cache-Control:", len(bench.fake.api_calls()))
    assert len(bench.fake.api_calls()) == 1


@pytest.mark.req("ENF-LEG-005", "ENF-LEG-007")
def test_cache_survives_restart(bench) -> None:
    bench.fake.cache_control = "max-age=86400"
    bench.enrich()
    bench.runtime = ProviderRuntime(clock=bench.time.now, sleep=bench.time.sleep)  # restart
    bench.enrich()
    print("api calls across restart:", len(bench.fake.api_calls()))
    assert len(bench.fake.api_calls()) == 1


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/c.png",
        "http://127.0.0.1/c.png",
        f"https://{COVER_HOST}@evil.example/c.png",
    ],
)
@pytest.mark.req("ENF-LEG-005", "ENF-LEG-007")
def test_cover_redirect_off_rules_is_not_followed(bench, target) -> None:
    orig = bench.fake._handle

    def h(r):
        if r.headers["host"] == COVER_HOST and r.url.path.startswith("/b/isbn/"):
            return httpx2.Response(302, headers={"location": target})
        return orig(r)

    bench.fake._handle = h  # type: ignore[method-assign]
    report = bench.enrich()
    hosts = {s.host for s in bench.fake.seen}
    print(target, report.outcome, report.cover, hosts)
    assert report.cover is None and "evil.example" not in hosts and "127.0.0.1" not in hosts


@pytest.mark.req("ENF-LEG-005", "ENF-LEG-007")
def test_plugin_cannot_set_user_agent_by_header_variants(bench) -> None:
    f = bench.fetcher()
    for hdr in ("user-agent", "USER-AGENT", "User-Agent"):
        asyncio.run(
            f.get(
                f"https://openlibrary.org/api/books?bibkeys=ISBN:{ISBN}&format=json&jscmd=data&{hdr}",
                headers={hdr: "spoof"},
            )
        )
    assert all("spoof" not in (s.user_agent or "") for s in bench.fake.seen)
