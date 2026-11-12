"""A bench for the Wikisource source (M2 L24): the real source over the real Fetcher, the real
registry, catalog and a real library on disk, against a scripted MediaWiki API and a scripted
export tool. Nothing here goes to a real network.

Hosts of the scripted resolver, and which of them the DomainRules name:

- `fr.wikisource.org` answers the API at `/w/api.php` (named, under `/w/`);
- `ws-export.wmcloud.org` answers the EPUB export (named);
- `en.wikipedia.org` and `fr.wikisource.evil.example` resolve, so that a refusal is the
  DomainRules' and not a DNS failure; every request that reaches them is recorded and the tests
  say it stays empty.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import httpx2
import sqlalchemy as sa

from shelfhold.acquisition.public import NativeAcquirer
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.fetcher import Network
from shelfhold.plugin_host import attestation
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.plugin_host.source_fetcher import SourceFetchers
from shelfhold.sdk.refs import SeriesRef
from tests.fakes.wikisource import Page, info_answer, search_answer
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.follow_env import empty_catalog
from tests.gutenberg_env import FakeTime, RoutedStore, host_of
from tests.library_env import LibEnv, lib_env
from tests.null_tombstones import NO_CATALOG_HITS

KEY = "wikisource"
API_HOST = "fr.wikisource.org"
EXPORT_HOST = "ws-export.wmcloud.org"
OUTSIDE_HOSTS = ("en.wikipedia.org", "fr.wikisource.evil.example")
PUBLIC = "93.184.216.34"
PAGE_URL = f"https://{API_HOST}/wiki/Le_Comte_de_Test"

BOOK = "Le Comte de Test"
BOOKS = {
    BOOK: Page(BOOK, contributors=("Alice Synth", "Bruno Synth"), anonymous=2),
    "La Tulipe Noire": Page("La Tulipe Noire", contributors=("Carla Synth",)),
    "Utilisateur:Alice": Page("Utilisateur:Alice", ns=2),
}


@dataclass
class Wire:
    recorder: Recorder
    pages: dict[str, Page] = field(default_factory=lambda: dict(BOOKS))
    exports: dict[str, bytes] = field(default_factory=dict)  # page title -> EPUB bytes
    now: Callable[[], float] = lambda: 0.0
    stamps: list[float] = field(default_factory=list)

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        host, path = host_of(request), request.url.path
        self.stamps.append(self.now())
        query = dict(request.url.params)
        if path == "/robots.txt":
            return httpx2.Response(404)
        if host == API_HOST and path == "/w/api.php":
            if query.get("list") == "search":
                needle = query.get("srsearch", "").casefold()
                found = [p for p in self.pages.values() if needle in p.title.casefold()]
                return httpx2.Response(200, content=search_answer(found))
            title = query.get("titles", "").replace("_", " ")
            return httpx2.Response(200, content=info_answer(self.pages.get(title), title))
        if host == EXPORT_HOST and path == "/":
            title = query.get("page", "").replace("_", " ")
            if title in self.exports:
                return httpx2.Response(
                    200,
                    content=self.exports[title],
                    headers={"content-type": "application/epub+zip"},
                )
        return httpx2.Response(404)

    def to(self, host: str) -> list[httpx2.Request]:
        return [r for r in self.recorder.requests if host_of(r) == host]

    @property
    def outside(self) -> list[httpx2.Request]:
        return [r for r in self.recorder.requests if host_of(r) in OUTSIDE_HOSTS]


@dataclass
class Bench:
    lib: LibEnv
    plugin: Any
    wire: Wire
    registry: PluginRegistry
    fetchers: SourceFetchers
    acquirer: NativeAcquirer
    bus: EventBus
    time: FakeTime
    settings: dict[str, Any]

    def place(self, variant_id: str, extension: str) -> str:
        return str(self.lib.manga / "Livres" / f"{variant_id}{extension}")

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.lib.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def ref(self, title: str, lang: str = "fr") -> SeriesRef:
        return SeriesRef(source_key=KEY, remote_id=f"{lang}:{title.replace(' ', '_')}", title="")

    def run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def search(self, query: str, **filters: Any) -> list[str]:
        page = self.run(self.acquirer.search(KEY, query, filters=filters or None))
        return [r.remote_id for r in page.items]

    def acquire(self, title: str = BOOK, **kw: Any) -> tuple[Any, Any]:
        async def go() -> tuple[Any, Any]:
            prepared = await self.acquirer.prepare(self.ref(title), **kw)
            if prepared.request_id is None:  # the Variant already has its file
                return prepared, None
            outcome = await self.acquirer.fetch(
                prepared.request_id, RoutedStore(self.lib), self.place
            )
            return prepared, outcome

        return asyncio.run(go())


@contextmanager
def wikisource_bench(
    *,
    exports: dict[str, bytes] | None = None,
    pages: dict[str, Page] | None = None,
    settings: dict[str, Any] | None = None,
    plugin: Any = None,
) -> Iterator[Bench]:
    from shelfhold.plugin_host.adapters.native.wikisource import WikisourceSource

    chosen: dict[str, Any] = dict(settings or {})
    fake = FakeTime()
    with (
        patch.object(
            attestation, "FIRST_PARTY_PLUGIN_KEYS", attestation.FIRST_PARTY_PLUGIN_KEYS | {KEY}
        ),
        lib_env() as lib,
    ):
        empty_catalog(lib)
        holder: list[Wire] = []
        recorder = Recorder(lambda request: holder[0].respond(request))
        served = Wire(recorder, exports=dict(exports or {}))
        if pages is not None:
            served.pages = dict(pages)
        served.now = fake.clock
        holder.append(served)
        hosts = (API_HOST, EXPORT_HOST, *OUTSIDE_HOSTS)
        network = Network(
            resolver=FakeResolver({h: [PUBLIC] for h in hosts}), transport=recorder.transport
        )
        source = plugin or WikisourceSource()
        registry = PluginRegistry()
        registry.load_all(
            [PluginCandidate(key=KEY, origin=PluginOrigin.FIRST_PARTY, load=lambda: source)]
        )
        fetchers = SourceFetchers(network, pace_clock=fake.clock, pace_sleep=fake.sleep)
        clock = SystemClock()
        bus = EventBus(clock)
        (lib.tmp / "recv").mkdir()
        acquirer = NativeAcquirer(
            lib.engine, clock, bus, registry, fetchers, staging_dir=lib.tmp / "recv",
            max_bytes=8 * 1024 * 1024, source_settings=lambda _key: dict(chosen),
            work_screen=lambda: NO_CATALOG_HITS,
        )  # fmt: skip
        yield Bench(lib, source, served, registry, fetchers, acquirer, bus, fake, chosen)
