"""A bench for the one-shot acquisition (M2 L19): a synthetic native source over the real Fetcher,
the real plugin registry, the real catalog, the real stores and a real library on disk.

The source `native-books` lives at `books.example.org`; its files are served at `/files/` by a
scripted transport that records every request that reaches the wire. Nothing here goes to a real
network: the resolver is scripted and the transport is a mock.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx2
import sqlalchemy as sa

from shelfhold.acquisition.public import NativeAcquirer
from shelfhold.events.bus import EventBus
from shelfhold.export.application.locks import work_lock_for
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.library.public import PassthroughStore, UntrustedEpubStore
from shelfhold.plugin_host import attestation
from shelfhold.plugin_host.attestation import FIRST_PARTY_PLUGIN_KEYS
from shelfhold.plugin_host.registry import PluginRegistry
from shelfhold.plugin_host.source_fetcher import SourceFetchers
from shelfhold.sdk.age import AgeRating  # noqa: F401 (the SDK surface a plugin imports)
from shelfhold.sdk.models import API_VERSION as API
from shelfhold.sdk.models import Cursor, DomainRule, Page, Permissions, PluginKind, RateLimit
from shelfhold.sdk.refs import FileAsset, ImageRequest, SeriesInfo, SeriesRef, UnitInfo
from shelfhold.sdk.source import Capability, SourceManifest
from tests.fakes_fetcher import Recorder
from tests.follow_env import empty_catalog
from tests.library_env import LibEnv, lib_env
from tests.null_tombstones import NONE_PURGED
from tests.support.sources import first_party, no_work_hits, source_stack

KEY = "native-books"


HOST = "books.example.org"
EVIL = "files.evil.example"
BOOK = "book/42"
UNIT = "native-books:42:ebook"
PAGE = f"https://{HOST}/{BOOK}"


class NativeBooks:
    """The plugin: a search, a URL form, one book as a file. Everything goes through `f`."""

    def __init__(self, *, trust: str = "untrusted", file_url: str | None = None,
                 mime: str = "application/epub+zip", remote_id: str = BOOK,
                 domains: tuple[str, ...] = (HOST,), patterns: tuple[str, ...] = (f"https://{HOST}/book/*",),
                 filters: tuple[str, ...] = ("language",)) -> None:  # fmt: skip
        self.file_url = file_url or f"https://{HOST}/files/book-42.epub"
        self.mime = mime
        self.remote_id = remote_id
        self.calls: list[str] = []
        self._filters = filters
        self.manifest = SourceManifest(
            key=KEY, kind=PluginKind.SOURCE, version="1.0.0", api=API, name="Native books",
            langs=["en"], content_kinds={"file"}, url_patterns=list(patterns),
            capabilities=Capability.SEARCH | Capability.URL_RESOLVE,
            rate_limit=RateLimit(requests_per_second=5.0, concurrency=2),
            permissions=Permissions(
                domains=[DomainRule(pattern=d, purpose="api") for d in domains]
            ),
            content_trust=trust,  # type: ignore[arg-type]
        )  # fmt: skip

    def filters(self) -> list[Any]:
        from shelfhold.sdk.models import FilterSpec

        return [FilterSpec(key=k, kind="text", label=k) for k in self._filters]

    def _series(self) -> SeriesRef:
        return SeriesRef(source_key=KEY, remote_id=self.remote_id, title="Un livre de test")

    async def search(
        self,
        f: Any,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        self.calls.append(f"search:{q}")
        answer = await f.get(f"https://{HOST}/api/search", params={"q": q})
        found = json.loads(answer.body)["items"]
        refs = [SeriesRef(source_key=KEY, remote_id=i["id"], title=i["title"]) for i in found]
        return Page[SeriesRef](items=refs, next=None)

    async def resolve_url(self, f: Any, url: str) -> SeriesRef | UnitInfo | None:
        self.calls.append(f"resolve:{url}")
        return self._series()

    async def get_series(self, f: Any, ref: SeriesRef) -> SeriesInfo:
        return SeriesInfo(ref=ref, title="Un livre de test", authors=["Personne"])

    async def list_units(self, f: Any, ref: SeriesRef) -> list[UnitInfo]:
        return [UnitInfo(remote_unit_id=UNIT, source_order=1, kind="book", number=None)]

    async def fetch_unit(self, f: Any, ref: SeriesRef, u: UnitInfo) -> FileAsset:
        return FileAsset(
            request=ImageRequest(url=self.file_url), mime=self.mime, filename="whatever.cbz"
        )


@dataclass
class Wire:
    """What the scripted server answers, and what reached it."""

    recorder: Recorder
    files: dict[str, bytes] = field(default_factory=dict)
    robots: str | None = None  # the robots.txt of books.example.org; None: there is none (404)
    cache_control: str = "max-age=600"

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/robots.txt":
            if self.robots is None:
                return httpx2.Response(404)
            return httpx2.Response(200, content=self.robots.encode())
        if path == "/api/search":
            body = {"items": [{"id": BOOK, "title": "Un livre de test"}]}
            return httpx2.Response(200, json=body, headers={"cache-control": self.cache_control})
        if path in self.files:
            return httpx2.Response(200, content=self.files[path])
        return httpx2.Response(404)

    @property
    def paths(self) -> list[str]:
        return [f"{r.headers.get('host', r.url.host)}{r.url.path}" for r in self.recorder.requests]


class RoutedStore:
    """What `_LazyStore` does in production: an `.epub` goes to the EPUB store (the trust sort,
    the neutralizer, the original in /store), anything else to the passthrough store."""

    def __init__(self, env: LibEnv, before: Callable[[Path], None] | None = None) -> None:
        self._env = env
        self._before = before  # a test that changes the staged file between receive and filing

    def ingest(self, **arguments: Any) -> Any:
        env = self._env
        if self._before is not None:
            self._before(arguments["staged"])
        hold = work_lock_for(env.engine, clock=SystemClock(), timeout=0.0)
        if str(arguments["dest"]).lower().endswith(".epub"):
            return UntrustedEpubStore(
                env.engine, env.fsops, env.locate, env.data, store_dir=str(env.store),
                tombstones=NONE_PURGED, hold_work=hold,
            ).ingest(**arguments)  # fmt: skip
        return PassthroughStore(
            env.engine, env.fsops, env.locate, env.data, tombstones=NONE_PURGED, hold_work=hold
        ).ingest(**arguments)


@dataclass
class Bench:
    lib: LibEnv
    plugin: NativeBooks
    wire: Wire
    registry: PluginRegistry
    fetchers: SourceFetchers
    acquirer: NativeAcquirer
    bus: EventBus
    delivered: list[Any] = field(default_factory=list)

    def place(self, variant_id: str, extension: str) -> str:
        return str(self.lib.manga / "Livres" / f"{variant_id}{extension}")

    def store(self, before: Callable[[Path], None] | None = None) -> RoutedStore:
        return RoutedStore(self.lib, before)

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.lib.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def prepared_and_fetched(self, ref: SeriesRef | None = None, **kw: Any) -> Any:
        import asyncio

        async def go() -> Any:
            prepared = await self.acquirer.prepare(ref or self.plugin._series(), **kw)
            assert prepared.request_id is not None
            outcome = await self.acquirer.fetch(prepared.request_id, self.store(), self.place)
            return prepared, outcome

        return asyncio.run(go())


@contextmanager
def acquire_bench(
    plugin: NativeBooks | None = None,
    *,
    files: dict[str, bytes] | None = None,
    robots: str | None = None,
    hosts: tuple[str, ...] = (HOST, EVIL),
    cache_enabled: bool = True,
    known: bool = True,
    work_screen: Any = no_work_hits,
) -> Iterator[Bench]:
    plugin = plugin or NativeBooks()
    # `known`: the host's own plugin (a `publisher` value is taken as declared); otherwise a plugin
    # it does not know, whose `publisher` is taken only after the admin attested it
    keys = FIRST_PARTY_PLUGIN_KEYS | {KEY} if known else FIRST_PARTY_PLUGIN_KEYS
    with patch.object(attestation, "FIRST_PARTY_PLUGIN_KEYS", keys), lib_env() as lib:
        empty_catalog(lib)
        delivered: list[Any] = []
        stack = source_stack(
            lib,
            lambda recorder: Wire(recorder, files=dict(files or {}), robots=robots),
            hosts=hosts,
            candidates=[first_party(KEY, lambda: plugin)],
            fetchers=lambda net: SourceFetchers(net, cache_enabled=cache_enabled),
            work_screen=work_screen,
            subscribe={"unit.stored": delivered.append},
        )
        yield Bench(
            lib,
            plugin,
            stack.wire,
            stack.registry,
            stack.fetchers,
            stack.acquirer,
            stack.bus,
            delivered,
        )
