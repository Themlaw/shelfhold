"""A bench for the generic OPDS source (M2 L22): the real `OpdsSource` over the real Fetcher, the
real plugin registry, catalog and stores, against a scripted server at `opds.example.org`.

The server's documents are the synthetic ones of `tests.fakes.opds`. Nothing goes to a real
network: the resolver is scripted and the transport is a mock that records each request, with the
headers that reached the wire. Secrets are drawn at random for each bench (nothing literal).
"""

from __future__ import annotations

import secrets
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
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.fetcher import Network
from shelfhold.plugin_host import attestation
from shelfhold.plugin_host.attestation import FIRST_PARTY_PLUGIN_KEYS
from shelfhold.plugin_host.native_sources import opds_source
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.plugin_host.source_fetcher import SourceFetchers
from tests.acquire_env import RoutedStore
from tests.fakes.opds import (
    ATOM,
    OPENSEARCH,
    acquisition_entry,
    atom_feed,
    kavita_description,
    komga_description,
    navigation_entry,
)
from tests.fakes.opds.feeds import link
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.follow_env import empty_catalog
from tests.library_env import LibEnv, lib_env
from tests.null_tombstones import NO_CATALOG_HITS

KEY = "opds"
HOST = "opds.example.org"
ORIGIN = f"https://{HOST}"
EVIL = "files.evil.example"
PUBLIC = "93.184.216.34"

Handler = Callable[[httpx2.Request], httpx2.Response]


def document(body: bytes, mime: str, *, cache: bool = True) -> Handler:
    headers = {"content-type": mime}
    if cache:
        headers["cache-control"] = "max-age=600"
    return lambda _request: httpx2.Response(200, content=body, headers=headers)


def file(body: bytes, mime: str = "application/epub+zip") -> Handler:
    return lambda _request: httpx2.Response(200, content=body, headers={"content-type": mime})


def redirect(location: str, status: int = 302) -> Handler:
    return lambda _request: httpx2.Response(status, headers={"location": location})


@dataclass
class Wire:
    """The scripted server: a handler per path. What reaches it is recorded."""

    recorder: Recorder
    routes: dict[str, Handler] = field(default_factory=dict)
    robots: str | None = None

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/robots.txt":
            if self.robots is None:
                return httpx2.Response(404)
            return httpx2.Response(200, content=self.robots.encode())
        handler = self.routes.get(path)
        return handler(request) if handler is not None else httpx2.Response(404)

    def serve(self, path: str, handler: Handler) -> None:
        self.routes[path] = handler

    def sent_to(self, host: str) -> list[httpx2.Request]:
        return [r for r in self.recorder.requests if r.url.host == host]

    @property
    def paths(self) -> list[str]:
        return [f"{r.url.host}{r.url.path}" for r in self.recorder.requests]

    def everything(self) -> str:
        """Every byte of every request as it reached the wire (URL and headers)."""
        return "\n".join(
            f"{r.method} {r.url}\n" + "\n".join(f"{k}: {v}" for k, v in r.headers.items())
            for r in self.recorder.requests
        )


@dataclass
class Bench:
    lib: LibEnv
    wire: Wire
    registry: PluginRegistry
    fetchers: SourceFetchers
    acquirer: NativeAcquirer
    settings: dict[str, Any]
    secrets: dict[str, str]
    bus: EventBus
    delivered: list[Any] = field(default_factory=list)

    def place(self, variant_id: str, extension: str) -> str:
        return str(self.lib.manga / "Livres" / f"{variant_id}{extension}")

    def store(self) -> RoutedStore:
        return RoutedStore(self.lib)

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.lib.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def database_text(self) -> str:
        """Every column of every table, as text: what a leak would be found in."""
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

    def files_text(self) -> str:
        parts: list[str] = []
        for root in (self.lib.tmp,):
            for path in Path(root).rglob("*"):
                if path.is_file() and path.suffix in {".json", ".log", ".txt", ".toml"}:
                    parts.append(path.read_text("utf-8", "replace"))
        return "\n".join(parts)


def new_key() -> str:
    """A credential drawn for one bench: no literal secret in the tests."""
    return secrets.token_urlsafe(24)


@contextmanager
def opds_bench(
    *,
    auth: str = "none",
    username: str = "lecteur",
    password: str | None = None,
    api_key: str | None = None,
    base_path: str = "/opds",
    hosts: tuple[str, ...] = (HOST, EVIL),
    cache_enabled: bool = True,
    extra_settings: dict[str, Any] | None = None,
) -> Iterator[Bench]:
    keys = FIRST_PARTY_PLUGIN_KEYS | {KEY}
    with patch.object(attestation, "FIRST_PARTY_PLUGIN_KEYS", keys), lib_env() as lib:
        empty_catalog(lib)
        holder: list[Wire] = []
        recorder = Recorder(lambda request: holder[0].respond(request))
        wire = Wire(recorder)
        holder.append(wire)
        network = Network(
            resolver=FakeResolver({h: [PUBLIC] for h in hosts}), transport=recorder.transport
        )
        registry = PluginRegistry()
        registry.load_all(
            [PluginCandidate(key=KEY, origin=PluginOrigin.FIRST_PARTY, load=opds_source)]
        )
        fetchers = SourceFetchers(network, cache_enabled=cache_enabled)
        clock = SystemClock()
        bus = EventBus(clock)
        delivered: list[Any] = []
        bus.subscribe(delivered.append, event_type="unit.stored")
        (lib.tmp / "recv").mkdir()
        settings: dict[str, Any] = {
            "base_url": f"{ORIGIN}{base_path}",
            "auth": auth,
            **(extra_settings or {}),
        }
        held: dict[str, str] = {}
        if auth == "basic":
            settings["username"] = username
            held["password"] = password or new_key()
        elif auth.startswith("api_key"):
            held["api_key"] = api_key or new_key()
        acquirer = NativeAcquirer(
            lib.engine, clock, bus, registry, fetchers, staging_dir=lib.tmp / "recv",
            max_bytes=8 * 1024 * 1024,
            source_settings=lambda _key: dict(settings),
            source_secrets=lambda _key: dict(held),
            work_screen=lambda: NO_CATALOG_HITS,
        )  # fmt: skip
        yield Bench(lib, wire, registry, fetchers, acquirer, settings, held, bus, delivered)


# -- the two servers the source is meant for -------------------------------------------------

BOOK_ID = "urn:test:book:42"
SERIES_ID = "urn:test:series:7"


def serve_kavita(b: Bench, files: dict[str, bytes], *, key: str | None = None) -> str:
    """A Kavita-shaped catalogue: the API key is a segment of every path. Returns the root path.
    `files`: file name -> bytes, served under the series."""
    secret = key if key is not None else b.secrets.get("api_key", "")
    root = f"/api/opds/{secret}"
    wire = b.wire
    wire.serve(
        root,
        document(
            atom_feed(
                "urn:test:root", "Catalogue",
                [navigation_entry("urn:test:all", "Toutes les séries", f"{root}/series")],
                links=[link("search", f"{root}/search", OPENSEARCH)],
            ),
            ATOM,
        ),
    )  # fmt: skip
    wire.serve(
        f"{root}/search",
        document(kavita_description(f"{root}/series?query={{searchTerms}}"), OPENSEARCH),
    )
    wire.serve(
        f"{root}/series",
        document(
            atom_feed(
                "urn:test:found", "Résultats",
                [navigation_entry(SERIES_ID, "Un livre de test", f"{root}/series/7")],
            ),
            ATOM,
        ),
    )  # fmt: skip
    entries = [
        acquisition_entry(
            f"{BOOK_ID}:{n}", f"Un livre de test {n}", f"{root}/series/7/download/{name}",
            "application/epub+zip",
        )
        for n, name in enumerate(files, start=1)
    ]  # fmt: skip
    wire.serve(
        f"{root}/series/7", document(atom_feed(SERIES_ID, "Un livre de test", entries), ATOM)
    )
    for name, body in files.items():
        wire.serve(f"{root}/series/7/download/{name}", file(body))
    return root


def serve_komga(
    b: Bench, files: dict[str, bytes], *, expect: Callable[[httpx2.Request], bool] | None = None
) -> str:
    """A Komga-shaped catalogue: HTTP Basic, a description with two `Url`, paged results."""
    root = "/opds"
    wire = b.wire

    def guarded(handler: Handler) -> Handler:
        def check(request: httpx2.Request) -> httpx2.Response:
            if expect is not None and not expect(request):
                return httpx2.Response(401)
            return handler(request)

        return check

    wire.serve(
        root,
        guarded(
            document(
                atom_feed(
                    "urn:test:root", "Bibliothèque",
                    [navigation_entry("urn:test:all", "Séries", f"{root}/v1.2/series")],
                    links=[link("search", f"{root}/v1.2/search", OPENSEARCH)],
                ),
                ATOM,
            )
        ),
    )  # fmt: skip
    wire.serve(
        f"{root}/v1.2/search",
        guarded(
            document(komga_description(f"{root}/v1.2/series?search={{searchTerms}}"), OPENSEARCH)
        ),
    )
    wire.serve(
        f"{root}/v1.2/series",
        guarded(
            document(
                atom_feed(
                    "urn:test:found", "Résultats",
                    [navigation_entry(SERIES_ID, "Un livre de test", f"{root}/v1.2/series/7")],
                ),
                ATOM,
            )
        ),
    )  # fmt: skip
    entries = [
        acquisition_entry(
            f"{BOOK_ID}:{n}", f"Un livre de test {n}", f"{root}/v1.2/books/{n}/file/{name}",
            "application/epub+zip",
        )
        for n, name in enumerate(files, start=1)
    ]  # fmt: skip
    wire.serve(
        f"{root}/v1.2/series/7",
        guarded(document(atom_feed(SERIES_ID, "Un livre de test", entries), ATOM)),
    )
    for n, (name, body) in enumerate(files.items(), start=1):
        wire.serve(f"{root}/v1.2/books/{n}/file/{name}", guarded(file(body)))
    return root
