"""An adopted Kavita library next to a simulated Suwayomi, with the pairing services over both.

Real files and database. The Kavita library is adopted as L29 does; the Suwayomi follows the same
series with 51 chapters; the follow cycle receives into the inbox of the export, the pipeline
exports from there. No network: the fetcher is the simulated server's own transport.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa

from shelfhold.acquisition.public import FollowCycle, FollowReport, bounded_receiver
from shelfhold.events.bus import EventBus
from shelfhold.export.application.migration_detect import SwitchoverDetector, SwitchoverGate
from shelfhold.export.application.pairing import Pairing
from shelfhold.export.application.preflight import PermissionPreflight
from shelfhold.export.domain.physical_roots import RECEIVED_DIR
from shelfhold.infrastructure.fetcher.client import IntegrationFetcher
from shelfhold.infrastructure.fetcher.lan import LanTargets
from shelfhold.library.public import PassthroughStore
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.sdk.refs import SeriesRef
from tests.adopt_env import Adoption, adoption, comicinfo
from tests.export_env import ExportEnv, export_env
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher
from tests.fakes_fetcher import FakeResolver
from tests.follow_env import LAN_IP, new_chapter
from tests.null_tombstones import NONE_PURGED

SERIES = "Series Alpha"  # the series of the simulated Suwayomi, and the folder of the adopted one


@dataclass
class Paired:
    a: Adoption
    server: FakeSuwayomi
    service: SuwayomiService
    fetcher: FakeSuwayomiFetcher
    cycle: FollowCycle
    bus: EventBus
    detector: SwitchoverDetector
    gate: SwitchoverGate
    preflight: PermissionPreflight
    pairing: Pairing

    @property
    def env(self) -> ExportEnv:
        return self.a.env

    @property
    def destination(self) -> str:
        return self.env.destination_id

    @property
    def folder(self) -> Path:
        return self.env.root / SERIES

    def upstream(self, chapters: int) -> None:
        """The simulated Suwayomi lists chapters 1 to `chapters` of the series."""
        self.server.state["mangas"][0]["chapters"] = [
            new_chapter(5000 + n * 10, n) for n in range(1, chapters + 1)
        ]

    def adopt_files(self, names: list[str], *, info: bytes | None = None) -> int:
        return self.a.add_series(SERIES, names, info=info or comicinfo(SERIES, language="en"))

    def library(self) -> list[SeriesRef]:
        return asyncio.run(self.service.followed_series(self.fetcher))

    def cycle_once(self) -> FollowReport:
        return asyncio.run(self.cycle.run())

    def settle(self, cycles: int = 4) -> int:
        """Cycles until the queue of the simulated Suwayomi has delivered (it needs two looks)."""
        fetched = 0
        for _ in range(cycles):
            fetched += self.cycle_once().fetched
        return fetched

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.env.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def downloads(self) -> list[str]:
        return [r.path for r in self.server.requests_to("/api/v1/chapter/")]

    def events(self, event_type: str) -> list[dict[str, Any]]:
        return self.rows("SELECT payload FROM event WHERE type = :t", t=event_type)


class PurgedPlaces:
    """A tombstone gate that refuses the places of some chapters (main track, raw number)."""

    def __init__(self, *numbers: str) -> None:
        self.numbers = set(numbers)

    def check(self, candidate: object, *, root_id: str | None = None, **_: Any) -> Any:
        blocked = getattr(candidate, "number_raw", None) in self.numbers
        return SimpleNamespace(blocked=blocked)

    def check_content(self, hashes: object = (), **_: Any) -> bool:
        return False


@contextmanager
def paired_env(*, tombstones: Any = NONE_PURGED) -> Iterator[Paired]:
    with export_env() as env:
        a = adoption(env)
        server = FakeSuwayomi(auth_mode="none")
        lib = env.lib
        dns = FakeResolver({"suwayomi.lan": [LAN_IP]})
        lan = asyncio.run(LanTargets.declare(["suwayomi.lan:4567"], dns))
        guarded = IntegrationFetcher(dns, lan_targets=lan, transport=server.transport)
        fetcher = FakeSuwayomiFetcher(server)
        service = SuwayomiService(BASE_URL)
        bus = EventBus(env.clock)

        def place(unit_id: str, variant_id: str, filename: str) -> str:
            return str(lib.manga / RECEIVED_DIR / f"{variant_id}.cbz")

        async def no_sleep(seconds: float) -> None:
            env.clock.advance(seconds=seconds)

        cycle = FollowCycle(
            lib.engine, bus, env.clock, service, fetcher,
            store=PassthroughStore(
                lib.engine, lib.fsops, lib.locate, lib.data, tombstones=tombstones
            ),
            receive=bounded_receiver(guarded, staging_dir=lib.tmp, max_bytes=8 << 20),
            place=place, sleep=no_sleep, tombstones=tombstones,
        )  # fmt: skip
        detector = SwitchoverDetector(lib.engine, lib.fsops)
        gate = SwitchoverGate(lib.engine, detector, clock=env.clock)
        preflight = PermissionPreflight(lib.engine, lib.fsops, bus, clock=env.clock)
        env.pipeline.use_preflight(preflight)
        env.pipeline.use_switchover(gate)
        pairing = Pairing(lib.engine, preflight, gate, clock=env.clock, tombstones=tombstones)
        yield Paired(a, server, service, fetcher, cycle, bus, detector, gate, preflight, pairing)
