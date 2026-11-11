"""A bench for the Project Gutenberg source (M2 L23): the real source over the real Fetcher, the
real registry, the real catalog and a real library on disk. Nothing here goes to a real network.

Three hosts exist for the scripted resolver, and only two of them are the configured ones:

- `catalog.example.org` serves the catalogue (CSV or RDF) at the official feed path;
- `mirror.example.net` serves the books of the mirror;
- `www.gutenberg.org` is the website. It resolves, so that a refusal is the DomainRules' and not a
  DNS failure, and every request that reaches it is recorded: the tests say it stays empty.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from shelfhold.infrastructure.fetcher import Network
from shelfhold.infrastructure.schema import platform
from shelfhold.library.public import PassthroughStore, UntrustedEpubStore
from shelfhold.plugin_host import attestation
from shelfhold.plugin_host.adapters.native.gutenberg import GutenbergSource
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.plugin_host.source_fetcher import SourceFetchers
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.testing.corpus.trapped_epubs.build import epub_bytes, xhtml
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.follow_env import empty_catalog
from tests.library_env import LibEnv, lib_env
from tests.null_tombstones import NONE_PURGED

KEY = "gutenberg"
CATALOG_HOST = "catalog.example.org"
MIRROR_HOST = "mirror.example.net"
SITE_HOSTS = ("www.gutenberg.org", "gutenberg.org")
PUBLIC = "93.184.216.34"
CATALOG_URL = f"https://{CATALOG_HOST}/cache/epub/feeds/pg_catalog.csv"
MIRROR_URL = f"https://{MIRROR_HOST}/gutenberg"
CATALOG_PATH = "/cache/epub/feeds/pg_catalog.csv"

CSV_HEADER = "Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
CSV = (
    CSV_HEADER
    + '1001,Text,2001-01-01,A Study in Test,en,"Doe, Jane, 1850-1920",Tests,PR,\n'
    + '3001,Text,2002-02-02,The Protected Tale,en,"Roe, Richard, 1900-1960",Tests,PR,\n'
    + '4001,Text,2003-03-03,"<script>alert(1)</script>Odd <b>Title</b>",en,"Poe, Edgar, 1809-1849",,,\n'
    + '5001,Sound,2004-04-04,An Audio Reading,en,"Doe, Jane, 1850-1920",,,\n'
    + '6001,Text,2005-05-05,Le Comte de Test,fr,"Dumas, Alexandre, 1802-1870",,,\n'
    + '8001,Text,2006-06-06,No Dates,en,"Nobody, Anne",,,\n'
)

RDF_NS = (
    'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
    'xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/" '
    'xmlns:marcrel="http://id.loc.gov/vocabulary/relators/"'
)


def rdf_ebook(pg_id: int, title: str, lang: str, author: str, years: str, translator: str) -> str:
    birth, _, death = years.partition("-")
    translators = (
        f"<marcrel:trl><pgterms:agent><pgterms:name>{translator}</pgterms:name></pgterms:agent>"
        f"</marcrel:trl>"
        if translator
        else ""
    )
    return f"""<pgterms:ebook rdf:about="ebooks/{pg_id}">
  <dcterms:title>{title}</dcterms:title>
  <dcterms:publisher>Project Gutenberg</dcterms:publisher>
  <dcterms:creator><pgterms:agent><pgterms:name>{author}</pgterms:name>
    <pgterms:birthdate>{birth}</pgterms:birthdate><pgterms:deathdate>{death}</pgterms:deathdate>
  </pgterms:agent></dcterms:creator>
  {translators}
  <dcterms:language><rdf:Description><rdf:value>{lang}</rdf:value></rdf:Description></dcterms:language>
  <dcterms:type><rdf:Description><rdf:value>Text</rdf:value></rdf:Description></dcterms:type>
</pgterms:ebook>"""


def rdf_catalog(*ebooks: str) -> bytes:
    return f'<?xml version="1.0" encoding="utf-8"?><rdf:RDF {RDF_NS}>{"".join(ebooks)}</rdf:RDF>'.encode()


# Two published translations of one novel, by two translators, in the same language.
TRANSLATIONS_RDF = rdf_catalog(
    rdf_ebook(7001, "The Count of Test", "en", "Dumas, Alexandre", "1802-1870", "Ames, Alice"),
    rdf_ebook(7002, "The Count of Test", "en", "Dumas, Alexandre", "1802-1870", "Bell, Bruno"),
)

CLEAN = epub_bytes(xhtml("<p>chapitre</p>"))
TRAPPED = epub_bytes(xhtml("<p>chapitre</p><script>alert(1)</script>"))


def epub_path(pg_id: int) -> str:
    return f"/gutenberg/cache/epub/{pg_id}/pg{pg_id}.epub"


class FakeTime:
    """A monotonic clock that only the pacer's sleep moves, and what it was asked to wait."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@dataclass
class Wire:
    """What the scripted servers answer, and what reached them."""

    recorder: Recorder
    catalog: bytes = CSV.encode()
    catalog_path: str = CATALOG_PATH
    files: dict[str, bytes] = field(default_factory=dict)
    redirect_files_to: str | None = None  # the mirror answers every book with a redirection
    etag: str = '"cat-1"'
    robots: str | None = None
    now: Callable[[], float] = lambda: 0.0  # the bench's clock: when each request arrived
    stamps: list[float] = field(default_factory=list)

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        host, path = request.url.host, request.url.path
        self.stamps.append(self.now())
        if path == "/robots.txt":
            if self.robots is None:
                return httpx2.Response(404)
            return httpx2.Response(200, content=self.robots.encode())
        if host == CATALOG_HOST and path == self.catalog_path:
            if request.headers.get("if-none-match") == self.etag:
                return httpx2.Response(304, headers={"etag": self.etag})
            return httpx2.Response(200, content=self.catalog, headers={"etag": self.etag})
        if host == MIRROR_HOST and path.startswith("/gutenberg/cache/epub/"):
            if self.redirect_files_to is not None:
                return httpx2.Response(302, headers={"location": self.redirect_files_to})
            if path in self.files:
                return httpx2.Response(200, content=self.files[path])
        return httpx2.Response(404)

    def to(self, host: str) -> list[httpx2.Request]:
        return [r for r in self.recorder.requests if r.url.host == host]

    @property
    def site(self) -> list[httpx2.Request]:
        return [r for r in self.recorder.requests if r.url.host in SITE_HOSTS]


class RoutedStore:
    """What the production store does: an `.epub` takes the trust sort and the neutralizer."""

    def __init__(self, env: LibEnv) -> None:
        self._env = env

    def ingest(self, **arguments: Any) -> Any:
        env = self._env
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
    plugin: GutenbergSource
    wire: Wire
    registry: PluginRegistry
    fetchers: SourceFetchers
    acquirer: NativeAcquirer
    bus: EventBus
    time: FakeTime
    settings: dict[str, Any]
    warnings: list[Any] = field(default_factory=list)

    def place(self, variant_id: str, extension: str) -> str:
        return str(self.lib.manga / "Livres" / f"{variant_id}{extension}")

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.lib.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def ref(self, pg_id: int) -> SeriesRef:
        return SeriesRef(source_key=KEY, remote_id=str(pg_id), title="")

    def run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def acquire(self, pg_id: int, **kw: Any) -> tuple[Any, Any]:
        async def go() -> tuple[Any, Any]:
            prepared = await self.acquirer.prepare(self.ref(pg_id), **kw)
            assert prepared.request_id is not None
            outcome = await self.acquirer.fetch(
                prepared.request_id, RoutedStore(self.lib), self.place
            )
            return prepared, outcome

        return asyncio.run(go())

    def set_jurisdiction(self, country: str, *, confirmed: bool) -> None:
        with self.lib.engine.begin() as connection:
            row = connection.execute(sa.select(platform.instance.c.instance_id)).first()
            stamp = SystemClock().now() if confirmed else None
            if row is None:
                connection.execute(
                    sa.insert(platform.instance).values(
                        instance_id=self.lib.instance_id, jurisdiction=country,
                        jurisdiction_confirmed_at=stamp,
                    )
                )
            else:
                connection.execute(
                    sa.update(platform.instance).values(
                        jurisdiction=country, jurisdiction_confirmed_at=stamp
                    )
                )

    def sha(self, data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()


@contextmanager
def gutenberg_bench(
    *,
    wire: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    files: dict[str, bytes] | None = None,
    plugin: GutenbergSource | None = None,
    start_time: Callable[[], float] | None = None,
) -> Iterator[Bench]:
    chosen: dict[str, Any] = (
        {"catalog_url": CATALOG_URL, "mirror_url": MIRROR_URL} if settings is None else settings
    )
    fake = FakeTime()
    with patch.object(attestation, "FIRST_PARTY_PLUGIN_KEYS", attestation.FIRST_PARTY_PLUGIN_KEYS | {KEY}), lib_env() as lib:  # fmt: skip
        empty_catalog(lib)
        holder: list[Wire] = []
        recorder = Recorder(lambda request: holder[0].respond(request))
        served = Wire(recorder, files=dict(files or {}), **(wire or {}))
        served.now = fake.clock
        holder.append(served)
        hosts = (CATALOG_HOST, MIRROR_HOST, *SITE_HOSTS)
        network = Network(
            resolver=FakeResolver({h: [PUBLIC] for h in hosts}), transport=recorder.transport
        )
        source = plugin or GutenbergSource(clock=fake.clock)
        registry = PluginRegistry()
        registry.load_all(
            [PluginCandidate(key=KEY, origin=PluginOrigin.FIRST_PARTY, load=lambda: source)]
        )
        fetchers = SourceFetchers(network, pace_clock=fake.clock, pace_sleep=fake.sleep)
        clock = SystemClock()
        bus = EventBus(clock)
        bench_warnings: list[Any] = []
        bus.subscribe(bench_warnings.append, event_type="ops.rights_warning")
        (lib.tmp / "recv").mkdir()
        acquirer = NativeAcquirer(
            lib.engine, clock, bus, registry, fetchers, staging_dir=lib.tmp / "recv",
            max_bytes=8 * 1024 * 1024, source_settings=lambda _key: dict(chosen),
        )  # fmt: skip
        yield Bench(
            lib, source, served, registry, fetchers, acquirer, bus, fake, chosen, bench_warnings
        )


def manifest_dict(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
