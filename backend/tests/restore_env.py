"""A restored instance for the L35 tests (EF-INT-026): a real export tree with real sentinels, a
real migrated database that is backed up (`VACUUM INTO`) and put back, the real tombstone journal.

Nothing is mocked but the clock and `plugin/parse`. A "restore" swaps the database file for an
older copy while the files on disk (and their sentinels) stay as they are.
"""

import json
import shutil
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

from shelfhold.events.bus import EventBus
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.application.restore import (
    DeletionExecutor,
    RestoreAdopter,
    RestoreGuard,
    TombstoneScreen,
    UpstreamInventory,
)
from shelfhold.export.application.roots import LoadedRoots, load_roots
from shelfhold.export.domain.path_map import PathMap
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.instance import WriteSessions
from shelfhold.infrastructure.instance.generation import GenerationCounter
from shelfhold.infrastructure.schema import catalog, platform
from shelfhold.infrastructure.secrets.keyring import Keyring, KeyringStore
from shelfhold.library.tombstones import TombstoneService
from shelfhold.library.tombstones.screen import TombstoneScreenAdapter
from tests.export_env import ExportEnv, Series, export_env
from tests.fakes.kavita.plugin_parse import simulate_parse
from tests.intents_env import NOW


@dataclass
class RestoreEnv:
    ex: ExportEnv
    series: Series
    store: KeyringStore
    sessions: WriteSessions
    pipeline: ExportPipeline
    fsops: Fsops
    loaded: LoadedRoots
    bus: EventBus
    chapters: dict[str, str] = field(default_factory=dict)  # number -> unit id

    # -- the instance ----------------------------------------------------------------------

    @property
    def engine(self) -> sa.Engine:
        return self.ex.lib.engine

    @property
    def library(self) -> Path:
        return self.ex.lib.library

    @property
    def db_path(self) -> Path:
        database = self.engine.url.database
        assert database is not None
        return Path(database)

    def sentinel(self, root: Path | None = None) -> dict[str, object]:
        return json.loads(((root or self.library) / ".shelfhold-mount-id").read_text("utf-8"))

    def open_instance(self) -> None:
        """(Re)build everything that holds the engine and fsops, from the database as it is."""
        with self.engine.connect() as connection:
            rows = connection.execute(sa.select(platform.physical_root.c.path_entry))
            roots = [row.path_entry for row in rows]
            self.loaded = load_roots(connection, PathMap.same_path(roots))
        self.fsops = Fsops.open(self.loaded.path_map, self.loaded.expected)
        self.ex.lib.fsops = self.fsops  # one process, one Fsops: the fixtures write through it
        self.sessions = WriteSessions(
            self.fsops,
            instance_id=str(self.sentinel()["instance_id"]),
            clock=self.ex.clock,
            generations=GenerationCounter(self.engine),
        )
        self.sessions.enforce()
        self.pipeline = self._pipeline()

    def _pipeline(self) -> ExportPipeline:
        return ExportPipeline(
            self.engine,
            self.fsops,
            locate=self.loaded.locate,
            data_dir=self.ex.lib.data,
            clock=self.ex.clock,
            parse=simulate_parse,
            parse_verified=True,
            sessions=self.sessions,
        )

    # -- chapters and exports --------------------------------------------------------------

    def add_chapters(self, first: int, last: int, series: Series | None = None) -> None:
        target = series or self.series
        with self.sessions.open(str(self.library)):  # the fixtures write under a lease too
            for number in range(first, last + 1):
                self.chapters[str(number)] = self.ex.add_chapter(target, str(number))

    def export(self, pipeline: ExportPipeline | None = None) -> None:
        (pipeline or self.pipeline).run(self.ex.destination_id, job_id=new_ulid())

    def files(self) -> dict[str, str]:
        """Every file under the library, reserved ones included, with its content digest."""
        found: dict[str, str] = {}
        for path in sorted(self.library.rglob("*")):
            if path.is_file() and path.name != "lease" and ".staging" not in path.parts:
                found[str(path.relative_to(self.library))] = path.read_bytes().hex()[:64] + str(
                    path.stat().st_size
                )
        return found

    def managed(self) -> list[Path]:
        return self.ex.files()

    # -- backup and restore ----------------------------------------------------------------

    def backup(self, name: str = "backup.db") -> Path:
        target = self.ex.lib.tmp / name
        target.unlink(missing_ok=True)
        raw = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            raw.execute("VACUUM INTO ?", (str(target),))
        finally:
            raw.close()
        return target

    def restore(self, backup: Path) -> None:
        """The database goes back to the backup; the disk keeps what it has (EF-INT-026)."""
        path = self.db_path
        self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        shutil.copyfile(backup, path)
        self.ex.lib.engine = create_db_engine(path)
        self.open_instance()

    # -- the restore machinery under test --------------------------------------------------

    def guard(self) -> RestoreGuard:
        return RestoreGuard(self.engine, self.fsops, clock=self.ex.clock, bus=self.bus)

    def keyring(self) -> Keyring:
        return self.store.load()

    def tombstones(self, keyring: Keyring | None = None) -> TombstoneService:
        return TombstoneService(self.engine, keyring or self.keyring(), self.fsops, self.bus)

    def adopter(
        self,
        *,
        service: TombstoneService | None = None,
        screen: TombstoneScreen | None = None,
        upstream: UpstreamInventory | None = None,
        purger: DeletionExecutor | None = None,
    ) -> RestoreAdopter:
        chosen = screen or TombstoneScreenAdapter(service or self.tombstones())
        return RestoreAdopter(
            self.engine,
            self.fsops,
            chosen,
            clock=self.ex.clock,
            bus=self.bus,
            guard=self.guard(),
            upstream=upstream,
            purger=purger,
        )

    def events(self, event_type: str) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(platform.event.c.payload)
                .where(platform.event.c.type == event_type)
                .order_by(platform.event.c.id)
            ).scalars()
            return [dict(row) for row in rows]

    def record_states(self) -> dict[str, str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.text("SELECT rel_path, state FROM export_record ORDER BY rel_path")
            )
            return {row.rel_path: row.state for row in rows}

    def lineage_of(self, rel_path: str) -> list[tuple[str, str, str | None]]:
        """(unit id, variant remote id, team id) of the units a manifest line covers."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT c.unit_id, v.remote_unit_id, v.team_id FROM export_record r "
                    "JOIN unit_coverage c ON c.carrier_type = 'export_item' "
                    "AND c.carrier_id = r.export_item_id "
                    "JOIN unit_variant v ON v.unit_id = c.unit_id WHERE r.rel_path = :p "
                    "ORDER BY c.position"
                ),
                {"p": rel_path},
            )
            return [(r.unit_id, r.remote_unit_id, r.team_id) for r in rows]

    def variant_ref(self, number: str) -> tuple[str, str, str]:
        """(source_key, remote_unit_id, content hash) as a purge would record them."""
        unit = self.chapters[number]
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT si.source_key, v.remote_unit_id, c.content_hash "
                    "FROM unit_variant v JOIN source_binding b ON b.id = v.source_binding_id "
                    "JOIN source_item si ON si.id = b.source_item_id "
                    "JOIN content c ON c.variant_id = v.id WHERE v.unit_id = :u"
                ),
                {"u": unit},
            ).one()
        return row.source_key, row.remote_unit_id, row.content_hash

    def edition(self) -> sa.Row[tuple[object, ...]]:
        with self.engine.connect() as connection:
            return connection.execute(
                sa.select(catalog.edition).where(catalog.edition.c.id == self.series.edition_id)
            ).one()


@contextmanager
def restore_env(*, first_chapters: int = 2) -> Iterator[RestoreEnv]:
    """A shelf with a series whose first chapters are exported, under write sessions."""
    with export_env() as ex:
        series = ex.main_series()
        with ex.engine.begin() as connection:
            sentinel = json.loads((ex.lib.library / ".shelfhold-mount-id").read_text("utf-8"))
            connection.execute(
                sa.update(platform.instance).values(instance_id=sentinel["instance_id"])
            )
        data = ex.lib.data
        store = KeyringStore(data / "keyring", lock_path=data / "keyring.lock")
        store.create(Keyring.generate(NOW))
        env = RestoreEnv.__new__(RestoreEnv)
        env.ex, env.series, env.store, env.bus = ex, series, store, EventBus(ex.clock)
        env.chapters = {}
        with ex.engine.begin() as connection:  # the fixture's own chapter 1 has no archive
            connection.execute(sa.text("DELETE FROM unit_variant"))
            connection.execute(sa.text("DELETE FROM unit"))
        env.open_instance()
        env.add_chapters(1, first_chapters)
        env.export()
        yield env


def numbers(paths: Sequence[Path]) -> list[str]:
    return [p.name for p in paths]
