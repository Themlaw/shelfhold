"""Activity for `stats` (EF-INT-025).

`seed_activity` writes the events and replays of the guards and interventions indicators (what the
other services emit). The export side is never written by a test: `Delivery` runs the real
chain, a follow cycle against the simulated Suwayomi, the export pipeline, the arrival polling
against the simulated Kavita, with the clock moved by hand between the steps; `stats` reads what
that left.
"""

import asyncio
from datetime import datetime, timedelta

import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.arrival import ArrivalPoller, ArrivalReport
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, export, platform
from tests.export_env import ROOT_SUBPATH
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.fakes.kavita.plugin_parse import simulate_parse
from tests.follow_env import Bench
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)
from tests.kavita_env import RecordingEvents
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT


def _event(connection: sa.Connection, event_type: str, at: datetime) -> None:
    connection.execute(
        sa.insert(platform.event).values(
            type=event_type, schema_version=1, payload={}, occurred_at=at, visibility="ops"
        )
    )


def seed_activity(settings: Settings, *, now: datetime) -> None:
    """Waiting states: 4 `target.not_indexed` and 2 `target.foreign_writer_detected` (inside the
    window), one more 40 days old. One sentinel guard. Two replays of "A verifier" resolved, one
    still open, one resolved 40 days ago."""
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            work = add_work(connection)
            edition = add_edition(connection, work)
            binding = add_binding(connection, work, edition, add_source_item(connection))
            unit = add_unit(connection, edition, "1")
            variant = add_variant(connection, unit, binding, "ch-1")
            for _ in range(4):
                _event(connection, "target.not_indexed", now - timedelta(days=3))
            for _ in range(2):
                _event(connection, "target.foreign_writer_detected", now - timedelta(days=5))
            _event(connection, "target.not_indexed", now - timedelta(days=40))
            _event(connection, "mount.sentinel_missing", now - timedelta(days=2))
            _event(connection, "series.gap", now - timedelta(days=2))  # neither a guard nor a wait
            pending = [
                (2, "applied", now - timedelta(days=4)),
                (3, "applied", now - timedelta(days=6)),
                (4, "open", None),
                (5, "applied", now - timedelta(days=40)),
            ]
            for revision, state, resolved in pending:
                connection.execute(
                    sa.insert(acquisition.revision_pending).values(
                        id=new_ulid(),
                        variant_id=variant,
                        revision=revision,
                        nature="rewrite",
                        state=state,
                        resolved_at=resolved,
                    )
                )
    finally:
        engine.dispose()


class Delivery:
    """A series followed on the simulated Suwayomi, exported to a Kavita destination, polled."""

    def __init__(self, bench: Bench) -> None:
        self.bench = bench
        self.kavita = FakeKavita()
        self.events = RecordingEvents()
        lib = bench.lib
        self.client = KavitaClient(
            build_fetcher(self.kavita), BASE_URL, AdminKey(API_KEY), events=self.events,
            clock=bench.clock,
        )  # fmt: skip
        with lib.engine.begin() as connection:
            destination = connection.execute(
                sa.select(export.shelf_destination.c.id, export.shelf_destination.c.target_id)
            ).one()
            self.destination_id, target_id = destination
            library_id = new_ulid()
            connection.execute(
                sa.insert(platform.instance).values(instance_id=new_ulid(), jurisdiction="FR")
            )
            connection.execute(
                sa.update(export.export_target)
                .where(export.export_target.c.id == target_id)
                .values(path_map=[{"local": str(lib.manga), "remote": "/manga", "mode": "rw"}])
            )
            connection.execute(
                sa.update(export.shelf_destination)
                .where(export.shelf_destination.c.id == self.destination_id)
                .values(root_subpath=ROOT_SUBPATH, kavita_library_type="manga")
            )
            connection.execute(
                sa.insert(export.remote_library).values(
                    id=library_id, export_target_id=target_id, remote_ref=str(LIBRARY_ID)
                )
            )
            connection.execute(
                sa.insert(export.shelf_destination_remote_library).values(
                    shelf_destination_id=self.destination_id,
                    remote_library_id=library_id,
                    export_target_id=target_id,
                )
            )
        (lib.manga / ROOT_SUBPATH).mkdir()
        (lib.data / "tmp").mkdir(exist_ok=True)
        self.scans: list[str] = []
        self.pipeline = ExportPipeline(
            lib.engine, lib.fsops, locate=lib.locate, data_dir=lib.data, clock=bench.clock,
            parse=simulate_parse, request_scan=self.scans.append, parse_verified=True,
        )  # fmt: skip

    def baseline(self) -> None:
        """First cycle: the series is discovered and what is already upstream is only known."""
        self.bench.run()

    def new_chapter(self, number: int) -> None:
        """A chapter appears upstream: the cycle detects it, requests it, then receives it."""
        self.bench.add_chapter(number)
        assert self.bench.run().requested == 1
        assert self.bench.run().fetched == 1

    def export(self) -> list[str]:
        self.pipeline.run(self.destination_id, job_id=new_ulid())
        with self.bench.lib.engine.connect() as connection:
            return sorted(connection.execute(sa.select(export.export_record.c.rel_path)).scalars())

    def kavita_indexes(self, rel_paths: list[str]) -> None:
        """Kavita indexes what is on disk once its folder has been scanned: a new series for a
        new folder, the new file joining the series that already lives in a known one."""
        for rel in rel_paths:
            remote = f"{REMOTE_ROOT}/{rel}"
            folder = remote.rsplit("/", 1)[0]
            if any(folder == series["folderPath"] for series in self.kavita.series):
                self.kavita.discover(folder, [remote])
            else:
                self.kavita.index_after_scans(folder.rsplit("/", 1)[-1], folder, [remote])
            asyncio.run(self.client.scan_folder(folder))

    def poll(self) -> ArrivalReport:
        poller = ArrivalPoller(self.bench.lib.engine, self.bench.clock, self.events)
        return asyncio.run(poller.poll(self.client, self.destination_id))
