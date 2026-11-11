"""M2 L25: the follow cycle over a real lncrawl adapter and the simulated instance.

What runs is the production stack end to end, with no network: the adapter, the credential injected
by the host (`AuthInjectingFetcher`), the guarded fetcher of an integration (LAN target, DomainRule
of the instance, no redirection out of its origin) over the instance's transport, the bounded
receiver, the EPUB check, the keeper of `/data/ranged`, the follow cycle and a real database.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from shelfhold.acquisition.application.lncrawl_ranges import epub_only
from shelfhold.acquisition.public import FollowCycle, FollowReport, bounded_receiver
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.infrastructure.fetcher.client import IntegrationFetcher
from shelfhold.infrastructure.fetcher.guard import DomainAllowlist
from shelfhold.infrastructure.fetcher.lan import LanTargets
from shelfhold.library.public import RangedKeeper
from shelfhold.plugin_host.adapters.meta.lncrawl import LncrawlService
from shelfhold.plugin_host.authflow import AuthInjectingFetcher
from shelfhold.sdk.models import AuthApiKey
from tests.fakes.lncrawl import API_KEY, BASE_URL, FakeLncrawl
from tests.fakes_clock import ManualClock
from tests.fakes_fetcher import FakeResolver
from tests.follow_env import LAN_IP, START, add_adult_shelf, empty_catalog
from tests.library_env import LibEnv, lib_env
from tests.null_tombstones import NONE_PURGED


@dataclass
class Bench:
    lib: LibEnv
    server: FakeLncrawl
    service: LncrawlService
    clock: ManualClock
    bus: EventBus
    cycle: FollowCycle
    keeper: RangedKeeper
    guarded: IntegrationFetcher
    delivered: list[PublishedEvent] = field(default_factory=list)

    def run(self, times: int = 1) -> FollowReport:
        report = FollowReport()
        for _ in range(times):
            report = asyncio.run(self.cycle.run())
        return report

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.lib.engine.connect() as connection:
            return [dict(r._mapping) for r in connection.execute(sa.text(sql), params)]

    def events(self, event_type: str) -> list[dict[str, Any]]:
        return [dict(e.payload) for e in self.delivered if e.event_type == event_type]

    def kept_files(self) -> list[Path]:
        folder = self.lib.data / "ranged"
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def finish(self) -> None:
        """Enough cycles for every ticket to be discovered, asked, polled and received."""
        self.run(5)


@contextmanager
def lncrawl_bench(
    server: FakeLncrawl | None = None, *, start: datetime = START, **service_options: Any
) -> Iterator[Bench]:
    server = server or FakeLncrawl()
    with lib_env() as lib:
        add_adult_shelf(lib)
        empty_catalog(lib)  # a fresh installation: the cycle discovers the Work and its Edition
        clock = ManualClock(start)
        dns = FakeResolver({"lncrawl.lan": [LAN_IP]})
        lan = asyncio.run(LanTargets.declare(["lncrawl.lan:8080"], dns))
        service = LncrawlService(BASE_URL, poll_delay=0.0, **service_options)
        allowlist = DomainAllowlist(
            "lncrawl", service.manifest.permissions.domains, {"base_url": BASE_URL}
        )
        guarded = IntegrationFetcher(
            dns, lan_targets=lan, transport=server.transport, domains=allowlist
        )
        fetcher = AuthInjectingFetcher(
            guarded, AuthApiKey(location="header", name="X-API-Key"), {"api_key": API_KEY},
            origin=BASE_URL,
        )  # fmt: skip
        keeper = RangedKeeper(lib.data)
        bus = EventBus(clock)

        def build() -> FollowCycle:
            return FollowCycle(
                lib.engine, bus, clock, service, fetcher,
                store=None,  # type: ignore[arg-type]
                receive=bounded_receiver(
                    guarded, staging_dir=lib.tmp, max_bytes=8 << 20, authorize=fetcher.authorize
                ),
                place=lambda *_: "unused", keep_range=epub_only(keeper), tombstones=NONE_PURGED,
            )  # fmt: skip

        bench = Bench(lib, server, service, clock, bus, build(), keeper, guarded)
        bus.subscribe(bench.delivered.append)
        yield bench
