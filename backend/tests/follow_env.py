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
from typing import Any, Literal

import sqlalchemy as sa

from shelfhold.acquisition.public import FollowCycle, FollowReport, bounded_receiver
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.export.domain.naming import DEFAULT_TEMPLATE
from shelfhold.infrastructure.fetcher.client import IntegrationFetcher
from shelfhold.infrastructure.fetcher.lan import LanTargets
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export
from shelfhold.infrastructure.wake import Wakes
from shelfhold.library.public import PassthroughStore
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.sdk.connected import ConnectedCapability
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher
from tests.fakes_clock import ManualClock
from tests.fakes_fetcher import FakeResolver
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.library_env import LibEnv, lib_env
from tests.null_tombstones import NONE_PURGED

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
    shelf_id: str = ""  # the adult shelf, when the bench was asked for one

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


def empty_catalog(lib: LibEnv) -> None:
    """A database with no Work at all: the unrelated chain `lib_env` seeds is removed."""
    chain = lib.chain
    with lib.engine.begin() as connection:
        for table, key in (
            (catalog.unit_variant, "variant"), (catalog.unit, "unit"),
            (acquisition.source_binding, "binding"), (catalog.edition, "edition"),
            (catalog.work, "work"),
        ):  # fmt: skip
            connection.execute(sa.delete(table).where(table.c.id == chain[key]))
        connection.execute(sa.text("DELETE FROM source_item"))


def add_adult_shelf(lib: LibEnv, *, max_age_rating: int | None = None) -> str:
    """A target, a shelf without ceiling (adult) and its destination: where a discovered series is
    filed. Returns the shelf id."""
    target_id, shelf_id, destination_id = new_ulid(), new_ulid(), new_ulid()
    with lib.engine.begin() as connection:
        connection.execute(
            sa.insert(export.export_target).values(
                id=target_id, kind="kavita", name="Kavita", path_map=[]
            )
        )
        connection.execute(
            sa.insert(export.shelf).values(
                id=shelf_id, name="Adultes", phrase="Mes mangas", max_age_rating=max_age_rating
            )
        )
        connection.execute(
            sa.insert(export.shelf_destination).values(
                id=destination_id,
                shelf_id=shelf_id,
                target_id=target_id,
                transport="tree",
                egress="foyer",
                root_subpath="manga",
                template=DEFAULT_TEMPLATE.dumps(),
                template_origin=f"default@{DEFAULT_TEMPLATE.version}",
            )
        )
    return shelf_id


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
    seeded: bool = True,
    adult_shelf: bool = False,
    wakes: Callable[[sa.Engine, ManualClock, EventBus], Wakes] | None = None,
    check_mode: Literal["always_on", "piloted"] = "always_on",
    stall_after: timedelta | None = None,
    requeue_cap: int | None = None,
) -> Iterator[Bench]:
    """`seeded=False`: a fresh installation, no Work and no binding (the cycle discovers them);
    `adult_shelf`: an adult shelf with a destination exists. `wakes`: builds the coordinator of the
    WakeHook (the tool is then reachable through it: `suwayomi`); `check_mode`: EF-SRC-025."""
    server = server or FakeSuwayomi(auth_mode="none")
    with lib_env() as lib:
        work_id = edition_id = binding_id = ""
        shelf_id = add_adult_shelf(lib) if adult_shelf else ""
        if not seeded:
            empty_catalog(lib)
        with lib.engine.begin() as connection:
            if seeded:
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
                    sa.insert(acquisition.series_follow).values(
                        edition_id=edition_id, work_id=work_id
                    )
                )
        clock = ManualClock(START)
        dns = FakeResolver({"suwayomi.lan": [LAN_IP]})
        lan = asyncio.run(LanTargets.declare(["suwayomi.lan:4567"], dns))
        guarded = IntegrationFetcher(dns, lan_targets=lan, transport=server.transport)
        fetcher = FakeSuwayomiFetcher(server)
        fetcher.has_credentials = has_credentials  # type: ignore[attr-defined]
        service = service or SuwayomiService(
            BASE_URL, now=clock.now,
            refresh_mode="driven" if check_mode == "piloted" else "always_on",
        )  # fmt: skip
        bus = EventBus(clock)
        coordinator = wakes(lib.engine, clock, bus) if wakes is not None else None
        if coordinator is not None:
            service.wake_target = "suwayomi"
            service.connected |= ConnectedCapability.WAKE
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(seconds=seconds)

        guard = tombstones(lib) if tombstones is not None else NONE_PURGED

        knobs: dict[str, Any] = {}
        if stall_after is not None:
            knobs["stall_after"] = stall_after
        if requeue_cap is not None:
            knobs["requeue_cap"] = requeue_cap

        def new_cycle() -> FollowCycle:
            return FollowCycle(
                lib.engine, bus, clock, service, fetcher,
                store=PassthroughStore(
                    lib.engine, lib.fsops, lib.locate, lib.data, tombstones=guard
                ),
                receive=bounded_receiver(guarded, staging_dir=lib.tmp, max_bytes=8 << 20),
                place=_placement(lib), sleep=sleep, stale_k=stale_k, max_attempts=max_attempts,
                tombstones=guard, wakes=coordinator, check_mode=check_mode, **knobs,
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
        bench.shelf_id = shelf_id
        bus.subscribe(bench.delivered.append)
        yield bench
