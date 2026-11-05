"""A follow-cycle bench: real SQLite, real /library files, the simulated Suwayomi (no network).

The series `Series Alpha` of the simulated server is bound to a fresh Edition; the cycle runs
against the real `PassthroughStore` and the real bounded receiver, over the fake's HTTP transport.
"""

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from shelfhold.acquisition.public import FollowCycle, FollowReport, bounded_receiver
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.infrastructure.fetcher.client import IntegrationFetcher
from shelfhold.infrastructure.fetcher.lan import LanTargets
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition
from shelfhold.library.public import PassthroughStore
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher
from tests.fakes_clock import ManualClock
from tests.fakes_fetcher import FakeResolver
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.library_env import LibEnv, lib_env

LAN_IP = "192.168.1.50"
START = datetime.fromisoformat("2026-11-05T09:00:00+00:00")


def new_chapter(chapter_id: int, number: int, url: str | None = None) -> dict[str, Any]:
    return {
        "id": chapter_id,
        "url": url or f"/series-alpha/ch-{number:03d}",
        "name": f"Chapter {number}",
        "chapterNumber": float(number),
        "sourceOrder": number - 1,
        "scanlator": "Synthetic Team A",
        "uploadDate": "1760000000000",
        "cbz": "comic_a_002.cbz",
    }


@dataclass
class Bench:
    lib: LibEnv
    server: FakeSuwayomi
    fetcher: FakeSuwayomiFetcher
    service: SuwayomiService
    clock: ManualClock
    bus: EventBus
    cycle: FollowCycle
    new_cycle: Callable[[], FollowCycle]
    work_id: str
    edition_id: str
    binding_id: str
    delivered: list[PublishedEvent] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    def run(self) -> FollowReport:
        return asyncio.run(self.cycle.run())

    def alpha(self) -> dict[str, Any]:
        return self.server.state["mangas"][0]

    def add_chapter(self, number: int) -> None:
        self.alpha()["chapters"].append(new_chapter(5000 + number * 10, number))

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.lib.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def binding(self) -> dict[str, Any]:
        """The followed binding (the bench's library also holds an unrelated one)."""
        return self.rows(
            "SELECT active, disabled_reason FROM source_binding WHERE id = :b", b=self.binding_id
        )[0]

    def add_binding(self, source_key: str, remote_id: str) -> str:
        """Follow another series of the same service (its own Edition, binding and follow)."""
        with self.lib.engine.begin() as connection:
            work_id = add_work(connection)
            edition_id = add_edition(connection, work_id)
            item_id, binding_id = new_ulid(), new_ulid()
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=item_id, plugin_id="suwayomi", source_key=source_key, remote_id=remote_id
                )
            )
            connection.execute(
                sa.insert(acquisition.source_binding).values(
                    id=binding_id, work_id=work_id, edition_id=edition_id,
                    source_item_id=item_id, priority=1, active=True,
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(acquisition.series_follow).values(edition_id=edition_id, work_id=work_id)
            )
        return binding_id

    def events(self, event_type: str) -> list[dict[str, Any]]:
        return [
            {"id": e["id"], **json.loads(e["payload"])}
            for e in self.rows("SELECT id, payload FROM event WHERE type = :t", t=event_type)
        ]

    def library_files(self) -> list[Path]:
        return self.lib.files_under(self.lib.manga)

    def set_first_seen(self, days_ago: dict[str, float]) -> None:
        """Date the discovery of each Variant (by remote chapter url suffix) back in time."""
        with self.lib.engine.begin() as connection:
            for suffix, days in days_ago.items():
                connection.execute(
                    sa.text(
                        "UPDATE unit_variant SET first_seen_at = :at "
                        "WHERE remote_unit_id LIKE :pattern"
                    ),
                    {"at": self.clock.now() - timedelta(days=days), "pattern": f"%{suffix}"},
                )


def _placement(lib: LibEnv) -> Any:
    def place(unit_id: str, variant_id: str, filename: str) -> str:
        return str(lib.manga / "Series Alpha" / filename)

    return place


@contextmanager
def follow_bench(
    server: FakeSuwayomi | None = None,
    *,
    stale_k: int = 3,
    has_credentials: bool = False,
    service: Any = None,
    max_attempts: int = 8,
    tombstones: Callable[[LibEnv], Any] | None = None,
) -> Iterator[Bench]:
    server = server or FakeSuwayomi(auth_mode="none")
    with lib_env() as lib:
        with lib.engine.begin() as connection:
            work_id = add_work(connection)
            edition_id = add_edition(connection, work_id)
            item_id, binding_id = new_ulid(), new_ulid()
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=item_id, plugin_id="suwayomi", source_key="suwayomi:7001",
                    remote_id="/series-alpha",
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(acquisition.source_binding).values(
                    id=binding_id, work_id=work_id, edition_id=edition_id,
                    source_item_id=item_id, priority=1, active=True,
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(acquisition.series_follow).values(edition_id=edition_id, work_id=work_id)
            )
        clock = ManualClock(START)
        dns = FakeResolver({"suwayomi.lan": [LAN_IP]})
        lan = asyncio.run(LanTargets.declare(["suwayomi.lan:4567"], dns))
        guarded = IntegrationFetcher(dns, lan_targets=lan, transport=server.transport)
        fetcher = FakeSuwayomiFetcher(server)
        fetcher.has_credentials = has_credentials  # type: ignore[attr-defined]
        service = service or SuwayomiService(BASE_URL)
        bus = EventBus(clock)
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(seconds=seconds)

        guard = tombstones(lib) if tombstones is not None else None

        def new_cycle() -> FollowCycle:
            return FollowCycle(
                lib.engine, bus, clock, service, fetcher,
                store=PassthroughStore(lib.engine, lib.fsops, lib.locate, lib.data),
                receive=bounded_receiver(guarded, staging_dir=lib.tmp, max_bytes=8 << 20),
                place=_placement(lib), sleep=sleep, stale_k=stale_k, max_attempts=max_attempts,
                tombstones=guard,
            )  # fmt: skip

        cycle = new_cycle()
        bench = Bench(
            lib,
            server,
            fetcher,
            service,
            clock,
            bus,
            cycle,
            new_cycle,
            work_id,
            edition_id,
            binding_id,
        )
        bench.sleeps = sleeps
        bus.subscribe(bench.delivered.append)
        yield bench
