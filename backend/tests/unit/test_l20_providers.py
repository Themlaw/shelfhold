"""M4 L20 (EF-BIB-013, ENF-LEG-005): AniList for lookups only, at the rate it announces.

AniList answers 30 requests a minute while its API is degraded and 90 in nominal times, and says
which in `X-RateLimit-Limit` (D13). The host's bucket follows the header, never goes above 90,
starts at 30 and keeps its balance in the database, so that several workers spend one quota.
Nothing of the account of anyone is read or written: a question is one search query. A fabricated
cassette answers; nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher.errors import DomainNotAllowed
from shelfhold.infrastructure.fetcher.quota_store import SqlBucketStore
from shelfhold.infrastructure.schema import metadata
from shelfhold.plugin_host.adapters.native import anilist as anilist_package
from shelfhold.plugin_host.adapters.native.anilist import AniListProvider, build_manifest
from shelfhold.plugin_host.provider_fetcher import ProviderFetcher, ProviderRuntime, open_provider
from shelfhold.sdk.metadata import (
    EntityRef,
    MatchHints,
    ProviderCapability,
    ProviderUnavailable,
)
from tests.fakes.anilist.server import HOST, TITLE, fake_anilist
from tests.fakes.cassette_server import CassetteServer

EDITION = "01HZZZZZZZZZZZZZZZZZZZZZZA"


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


class FakeTime:
    """The bucket's clock and sleep: waiting moves the clock, nobody really waits."""

    def __init__(self) -> None:
        self.t = 10_000.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def worker(server: CassetteServer, time: FakeTime, engine: Engine | None = None) -> ProviderFetcher:
    """One worker: its own runtime (bucket, cache), the database balance when there is one."""
    server.clock = time.now
    runtime = ProviderRuntime(clock=time.now, sleep=time.sleep, wall=time.now)
    if engine is not None:
        runtime.buckets.use_store(SqlBucketStore(engine))
    return open_provider(
        build_manifest(), server.resolver, contact=None, runtime=runtime,
        transport=server.transport,
    )  # fmt: skip


def ask(fetcher: ProviderFetcher) -> None:
    target = EntityRef(entity_type="edition", entity_id=EDITION)
    run(AniListProvider().identify(fetcher, target, MatchHints(title=TITLE)))


def busiest_minute(server: CassetteServer) -> int:
    """The most requests that arrived inside any window of 60 seconds."""
    times = [c.at for c in server.calls()]
    return max(sum(1 for x in times if start <= x < start + 60) for start in times)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    found = create_db_engine(tmp_path / "quota.db")
    metadata.create_all(found)
    yield found
    found.dispose()


# -- the rate follows what AniList announces ---------------------------------------------------


@pytest.mark.req("ENF-LEG-005", "EF-BIB-013")
def test_anilist_30_per_min_while_degraded() -> None:
    time = FakeTime()
    server = fake_anilist("search_degraded", clock=time.now)
    fetcher = worker(server, time)
    for _ in range(45):
        ask(fetcher)
    assert len(server.calls()) == 45
    assert busiest_minute(server) <= 30


@pytest.mark.req("ENF-LEG-005")
def test_anilist_bucket_starts_at_30_without_any_header() -> None:
    time = FakeTime()
    server = fake_anilist("search_nominal", clock=time.now, limit=0)  # announces nothing
    fetcher = worker(server, time)
    for _ in range(45):
        ask(fetcher)
    assert busiest_minute(server) <= 30


@pytest.mark.req("ENF-LEG-005")
def test_anilist_nominal_90_and_never_above() -> None:
    time = FakeTime()
    server = fake_anilist("search_nominal", clock=time.now)
    fetcher = worker(server, time)
    for _ in range(150):
        ask(fetcher)
    assert 30 < busiest_minute(server) <= 90  # it did speed up, within what it announced
    greedy = fake_anilist("search_nominal", clock=time.now, limit=500)  # an announcement above 90
    other = worker(greedy, time)
    for _ in range(150):
        ask(other)
    assert busiest_minute(greedy) <= 90


@pytest.mark.req("ENF-LEG-005")
def test_anilist_429_waits_for_retry_after() -> None:
    time = FakeTime()
    server = fake_anilist("rate_limited", clock=time.now)
    fetcher = worker(server, time)
    with pytest.raises(ProviderUnavailable):
        ask(fetcher)
    refused_at = server.calls()[-1].at
    server.status = 200  # the API answers again
    ask(fetcher)
    assert server.calls()[-1].at - refused_at >= 60


@pytest.mark.req("ENF-LEG-005")
def test_anilist_workers_share_one_budget(engine: Engine) -> None:
    time = FakeTime()
    server = fake_anilist("search_degraded", clock=time.now)
    workers = [worker(server, time, engine) for _ in range(3)]
    for n in range(45):
        ask(workers[n % 3])
    assert busiest_minute(server) <= 30


@pytest.mark.req("ENF-LEG-005")
def test_anilist_degradation_seen_by_one_worker_binds_the_others(engine: Engine) -> None:
    time = FakeTime()
    server = fake_anilist("search_nominal", clock=time.now)  # announces 90
    first, second = worker(server, time, engine), worker(server, time, engine)
    for _ in range(10):
        ask(first)
    server.limit = 30  # the API degrades; only the first worker hears it
    ask(first)
    mark = len(server.calls())
    server.limit = 0
    for _ in range(45):
        ask(second)  # never saw the 30: the database did
    later = CassetteServer(HOST, (), clock=time.now)
    later.seen = server.calls()[mark:]
    assert busiest_minute(later) <= 30


# -- lookup only: the account of nobody is read or written -------------------------------------

_ACCOUNT_WORDS = re.compile(
    r"mutation|viewer|medialist|savemedialist|toggle|following|notification|oauth|authorization",
    re.IGNORECASE,
)


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_anilist_lookup_only_no_tracking_sync() -> None:
    time = FakeTime()
    server = fake_anilist("search_degraded", clock=time.now)
    ask(worker(server, time))
    (call,) = server.calls()  # one question, one request: no crawl, no second page
    assert (call.host, call.method) == (HOST, "POST")
    assert "authorization" not in {name.lower() for name in call.headers}
    payload = json.loads(call.body)
    assert payload["query"].lstrip().startswith("query")  # never a mutation
    assert not _ACCOUNT_WORDS.search(payload["query"])  # nothing of an account
    assert set(payload["variables"]) <= {"search", "perPage"}
    assert payload["variables"]["perPage"] <= 10  # a lookup, never a collection
    manifest = build_manifest()
    assert manifest.auth is None and manifest.capabilities == ProviderCapability.IDENTIFY
    with pytest.raises(NotImplementedError):  # no login, no token, no refresh
        run(AniListProvider().login(worker(server, time), "oauth", {}))  # type: ignore[arg-type]
    source = " ".join(p.read_text() for p in Path(anilist_package.__file__).parent.glob("*.py"))
    assert not re.search(r"mutation|viewer|medialist|authorization|bearer", source, re.IGNORECASE)


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_anilist_reaches_only_its_graphql_host() -> None:
    time = FakeTime()
    server = fake_anilist("search_degraded", clock=time.now)
    fetcher = worker(server, time)
    assert [d.pattern for d in build_manifest().permissions.domains] == [HOST]
    with pytest.raises(DomainNotAllowed):  # the site of the accounts is not on the list
        run(fetcher.get("https://anilist.co/api/v2/oauth/token"))
    assert server.calls() == []
