"""A world for "Mettre à l'abri" and the upstream purge (L16): real files in /library and /store,
a migrated database, and a stand-in for the connected service that holds downloads (nothing
leaves the test). The service counts what it is asked to erase."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export, platform, storage
from shelfhold.library.public import (
    CycleReport,
    KeepCopier,
    PassthroughStore,
    Safeguard,
    SafeguardCycle,
    SafeguardInventory,
    StoreBudget,
    UpstreamPurge,
)
from shelfhold.safeguard_jobs import service_refusal
from shelfhold.sdk.connected import ReleaseResult, Ticket
from tests.fakes_clock import ManualClock
from tests.library_env import JPEG, PNG, LibEnv, cbz_bytes
from tests.null_tombstones import NONE_PURGED

NOW = datetime(2026, 11, 10, 3, 40, tzinfo=UTC)
ROOMY = StoreBudget(free_bytes=10**12, reserve_bytes=0)


@dataclass
class FakeUpstream:
    """The connected service: tickets it holds downloaded, and what it was asked to erase."""

    held: set[str] = field(default_factory=set)
    released: list[str] = field(default_factory=list)
    after_poll: Any = None  # called after every poll: a test's way to act "between" two calls
    wake_target = None

    async def poll(self, f: Any, t: Ticket) -> str:
        state = "ready" if t.token in self.held else "gone"
        if self.after_poll is not None:
            self.after_poll()
        return state

    async def release(self, f: Any, t: Ticket) -> ReleaseResult:
        if t.token not in self.held:
            return ReleaseResult(released=False)
        self.held.discard(t.token)
        self.released.append(t.token)
        return ReleaseResult(released=True)


@dataclass
class Item:
    unit_id: str
    variant_id: str
    library: Path
    data: bytes
    ticket: str
    request_id: str


class World:
    def __init__(self, env: LibEnv, *, option: bool = True, record_state: str = "scanned") -> None:
        self.env = env
        self.upstream = FakeUpstream()
        self.clock = ManualClock(NOW)
        self.events: list[tuple[str, dict[str, object]]] = []
        self.items: list[Item] = []
        self._first = True
        with env.engine.begin() as c:
            c.execute(
                sa.insert(platform.instance).values(instance_id=env.instance_id, jurisdiction="FR")
            )
            target, self.shelf_id, self.destination_id = new_ulid(), new_ulid(), new_ulid()
            c.execute(
                sa.insert(export.export_target).values(
                    id=target, kind="kavita", name="K", path_map=[]
                )
            )
            c.execute(
                sa.insert(export.shelf).values(
                    id=self.shelf_id, name="S", phrase="p", retention_mode="passthrough",
                    purge_upstream_after_export=option,
                )
            )  # fmt: skip
            c.execute(
                sa.insert(export.shelf_destination).values(
                    id=self.destination_id, shelf_id=self.shelf_id, target_id=target,
                    transport="tree", egress="foyer", template="t", template_origin="default@1",
                )
            )  # fmt: skip
        self.record_state = record_state
        self.journal = IntentJournal(env.engine, env.fsops, clock=self.clock)

    # -- building ------------------------------------------------------------------------------

    def add(self, *, owed: bool = False, exported: bool = True, fetched: bool = True) -> Item:
        env = self.env
        if self._first:
            unit_id, variant_id = env.chain["unit"], env.chain["variant"]
            self._first = False
        else:
            unit_id, variant_id = env.new_unit()
        n = len(self.items) + 1
        data = cbz_bytes((("001.png", PNG + str(n).encode()), ("002.jpg", JPEG)))
        dest = env.manga / "Dr Stone" / f"Dr Stone - {n}.cbz"
        PassthroughStore(
            env.engine, env.fsops, env.locate, env.data, tombstones=NONE_PURGED
        ).ingest(unit_id=unit_id, variant_id=variant_id, staged=env.stage(data), dest=str(dest))
        ticket, request_id = f"ticket-{n}", new_ulid()
        with env.engine.begin() as c:
            if fetched:
                c.execute(
                    sa.insert(acquisition.download_request).values(
                        id=request_id, target_kind="unit", variant_id=variant_id,
                        source_binding_id=env.chain["binding"], ticket=ticket, state="fetched",
                    )
                )  # fmt: skip
                self.upstream.held.add(ticket)
            if owed:
                c.execute(
                    sa.update(catalog.unit)
                    .where(catalog.unit.c.id == unit_id)
                    .values(availability="removed")
                )
            if exported:
                content_id = c.execute(
                    sa.select(storage.content.c.id).where(storage.content.c.unit_id == unit_id)
                ).scalar_one()
                item_id = new_ulid()
                c.execute(
                    sa.insert(export.export_item).values(
                        id=item_id, edition_id=env.chain["edition"], kind="unit",
                        content_id=content_id,
                    )
                )  # fmt: skip
                c.execute(
                    sa.insert(export.export_record).values(
                        shelf_destination_id=self.destination_id, export_item_id=item_id,
                        locator_type="none", state=self.record_state,
                    )
                )  # fmt: skip
        item = Item(unit_id, variant_id, dest, data, ticket, request_id)
        self.items.append(item)
        return item

    # -- the two halves --------------------------------------------------------------------------

    def keeper(self) -> KeepCopier:
        env = self.env
        safeguard = Safeguard(env.engine, env.fsops, env.locate, str(env.store), env.data)
        return KeepCopier(
            env.engine, env.fsops, env.locate, str(env.store), env.data, self.journal,
            extract=safeguard.run,
        )  # fmt: skip

    def cycle(self, budget: StoreBudget = ROOMY, gate: Any = NONE_PURGED) -> SafeguardCycle:
        def publish(connection: sa.Connection, event: str, payload: dict[str, object]) -> None:
            self.events.append((event, payload))

        return SafeguardCycle(
            self.env.engine, self.env.fsops, self.keeper(), gate, lambda: budget, publish,
            self.clock,
        )  # fmt: skip

    def run_cycle(self, budget: StoreBudget = ROOMY, gate: Any = NONE_PURGED) -> CycleReport:
        cycle = self.cycle(budget, gate)
        report = CycleReport()
        batch = cycle.plan(report)
        for step in cycle.steps(batch, report):
            step()
        cycle.close(report)
        return report

    def purge(self, gate: Any = NONE_PURGED, service_gate: Any = service_refusal) -> UpstreamPurge:
        return UpstreamPurge(
            self.env.engine, self.env.fsops, self.journal, gate, service_gate, self.upstream,
            object(), self.clock, ticket=lambda token: Ticket(token=token, target_kind="unit"),
        )  # fmt: skip

    def candidates(self) -> list[Any]:
        return SafeguardInventory(self.env.engine).upstream_candidates()

    # -- reading ---------------------------------------------------------------------------------

    def store_files(self) -> list[Path]:
        return self.env.files_under(self.env.store)

    def intents(self, op: str = "purge_upstream") -> list[Any]:
        from shelfhold.infrastructure.schema import coherence

        with self.env.engine.connect() as c:
            return list(
                c.execute(sa.select(coherence.fs_intent).where(coherence.fs_intent.c.op == op))
            )

    def ticket_of(self, item: Item) -> str | None:
        with self.env.engine.connect() as c:
            return c.execute(
                sa.select(acquisition.download_request.c.ticket).where(
                    acquisition.download_request.c.id == item.request_id
                )
            ).scalar_one()

    def pause_service(self) -> None:
        with self.env.engine.begin() as c:
            c.execute(
                sa.insert(acquisition.source_config).values(
                    plugin_id="suwayomi",
                    health={"pause": {"failure_class": "instance", "event_type": "source.x",
                                      "waiting_state": "source_instance_changed",
                                      "since": NOW.isoformat()}},
                )
            )  # fmt: skip

    def set_settings(self, **settings: object) -> None:
        with self.env.engine.begin() as c:
            row = c.execute(
                sa.select(acquisition.source_config.c.plugin_id).where(
                    acquisition.source_config.c.plugin_id == "suwayomi"
                )
            ).first()
            if row is None:
                c.execute(
                    sa.insert(acquisition.source_config).values(
                        plugin_id="suwayomi", settings=settings, health={}
                    )
                )
            else:
                c.execute(
                    sa.update(acquisition.source_config)
                    .where(acquisition.source_config.c.plugin_id == "suwayomi")
                    .values(settings=settings)
                )
