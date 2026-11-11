"""A bench for the Standard Ebooks source (M2 L24): the real source over the real Fetcher, the
real registry, catalog and a real library on disk, against a scripted `standardebooks.org`. Nothing
here goes to a real network.

Hosts of the scripted resolver: `standardebooks.org` (named by the DomainRules, the pages under
`/ebooks/` and the patron feed under `/feeds/`) and `files.evil.example`, which resolves so that a
refusal is the DomainRules' and not a DNS failure; what reaches it is recorded and the tests say it
stays empty. The credential is drawn at random for each bench: no literal secret in a test.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx2
import sqlalchemy as sa

from shelfhold.acquisition.public import NativeAcquirer
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.fetcher import Network
from shelfhold.plugin_host import attestation
from shelfhold.plugin_host.native_sources import first_party_native_candidates
from shelfhold.plugin_host.registry import PluginRegistry
from shelfhold.plugin_host.source_fetcher import SourceFetchers
from shelfhold.sdk.refs import SeriesRef
from tests.fakes.opds import ATOM, OPENSEARCH
from tests.fakes.standardebooks import (
    FEED_ROOT,
    download_path,
    patron_results,
    patron_root,
    search_doc,
)
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.follow_env import empty_catalog
from tests.gutenberg_env import FakeTime, RoutedStore, host_of
from tests.library_env import LibEnv, lib_env
from tests.opds_env import Wire, document, file

KEY = "standardebooks"
HOST = "standardebooks.org"
EVIL = "files.evil.example"
PUBLIC = "93.184.216.34"
SLUG = "jane-synth/le-test"
SLUG2 = "jane-synth/la-tulipe"


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@dataclass
class TimedWire(Wire):
    """The scripted server, noting when (on the bench's clock) each request arrived."""

    now: Any = None
    stamps: list[float] = field(default_factory=list)

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        self.stamps.append(self.now())
        return super().respond(request)


@dataclass
class Bench:
    lib: LibEnv
    wire: TimedWire
    registry: PluginRegistry
    fetchers: SourceFetchers
    acquirer: NativeAcquirer
    settings: dict[str, Any]
    secrets: dict[str, str]
    time: FakeTime

    def place(self, variant_id: str, extension: str) -> str:
        return str(self.lib.manga / "Livres" / f"{variant_id}{extension}")

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.lib.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def database_text(self) -> str:
        out: list[str] = []
        with self.lib.engine.connect() as connection:
            tables = [
                r[0]
                for r in connection.execute(
                    sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            ]
            for name in tables:
                for row in connection.execute(sa.text(f'SELECT * FROM "{name}"')):  # noqa: S608
                    out.append(repr(tuple(row)))
        return "\n".join(out)

    def ref(self, slug: str = SLUG) -> SeriesRef:
        return SeriesRef(source_key=KEY, remote_id=slug, title="")

    def serve_book(self, slug: str, body: bytes) -> None:
        self.wire.serve(download_path(slug), file(body))

    def serve_patron(self, slugs: list[str], *, next_href: str | None = None) -> None:
        """The patron feed: it answers only a request that carries the patron's credential."""
        user, password = self.settings.get("username", ""), self.secrets.get("password", "")

        def guarded(body: bytes, mime: str) -> Any:
            ok = document(body, mime)

            def handler(request: httpx2.Request) -> httpx2.Response:
                if request.headers.get("authorization") != basic(user, password):
                    return httpx2.Response(401)
                return ok(request)

            return handler

        self.wire.serve(FEED_ROOT, guarded(patron_root(), ATOM))
        self.wire.serve(f"{FEED_ROOT}/search", guarded(search_doc(), OPENSEARCH))
        self.wire.serve(
            f"{FEED_ROOT}/all", guarded(patron_results(slugs, next_href=next_href), ATOM)
        )
        self.wire.serve(f"{FEED_ROOT}/all2", guarded(patron_results(slugs), ATOM))

    def feed_calls(self) -> list[httpx2.Request]:
        return [r for r in self.wire.recorder.requests if r.url.path.startswith("/feeds/")]

    def book_calls(self) -> list[httpx2.Request]:
        return [
            r
            for r in self.wire.recorder.requests
            if r.url.path.startswith("/ebooks/") and r.url.path != "/robots.txt"
        ]

    def run(self, coro: Any) -> Any:
        import asyncio

        return asyncio.run(coro)

    def acquire(self, slug: str = SLUG, ref: SeriesRef | None = None) -> tuple[Any, Any]:
        async def go() -> tuple[Any, Any]:
            prepared = await self.acquirer.prepare(ref or self.ref(slug))
            if prepared.request_id is None:
                return prepared, None
            outcome = await self.acquirer.fetch(
                prepared.request_id, RoutedStore(self.lib), self.place
            )
            return prepared, outcome

        return self.run(go())


@contextmanager
def standardebooks_bench(
    *,
    auth: str = "none",
    username: str = "lectrice",
    password: str | None = None,
    extra_settings: dict[str, Any] | None = None,
) -> Iterator[Bench]:
    keys = attestation.FIRST_PARTY_PLUGIN_KEYS | {KEY}
    fake = FakeTime()
    with patch.object(attestation, "FIRST_PARTY_PLUGIN_KEYS", keys), lib_env() as lib:
        empty_catalog(lib)
        holder: list[TimedWire] = []
        recorder = Recorder(lambda request: holder[0].respond(request))
        wire = TimedWire(recorder, now=fake.clock)
        holder.append(wire)
        network = Network(
            resolver=FakeResolver({h: [PUBLIC] for h in (HOST, EVIL)}),
            transport=recorder.transport,
        )
        registry = PluginRegistry()
        registry.load_all([c for c in first_party_native_candidates() if c.key == KEY])
        fetchers = SourceFetchers(network, pace_clock=fake.clock, pace_sleep=fake.sleep)
        clock = SystemClock()
        bus = EventBus(clock)
        (lib.tmp / "recv").mkdir()
        settings: dict[str, Any] = {"auth": auth, **(extra_settings or {})}
        held: dict[str, str] = {}
        if auth == "basic":
            settings["username"] = username
            held["password"] = password or secrets.token_urlsafe(24)
        acquirer = NativeAcquirer(
            lib.engine, clock, bus, registry, fetchers, staging_dir=lib.tmp / "recv",
            max_bytes=8 * 1024 * 1024, source_settings=lambda _key: dict(settings),
            source_secrets=lambda _key: dict(held),
        )  # fmt: skip
        bench = Bench(lib, wire, registry, fetchers, acquirer, settings, held, fake)
        yield bench


def paths_of(b: Bench) -> list[str]:
    return [f"{host_of(r)}{r.url.path}" for r in b.wire.recorder.requests]


def files_text(root: Path) -> str:
    parts = []
    for path in Path(root).rglob("*"):
        if path.is_file() and path.suffix in {".json", ".log", ".txt", ".toml"}:
            parts.append(path.read_text("utf-8", "replace"))
    return "\n".join(parts)
