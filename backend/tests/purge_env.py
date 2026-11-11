"""A synthetic instance for the `purge --now` tests (L34): the exported library of `export_env`
(real files, real SQLite, real pipeline), the real tombstone service over a real keyring, and one
copy of the Unit in every place the purge must reach. Nothing is mocked: not the file system, not
SQLite, not the cipher."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

from shelfhold.events.bus import EventBus
from shelfhold.export.application.purge import PurgeService
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, platform, storage
from shelfhold.infrastructure.secrets.keyring import Keyring, KeyringStore
from shelfhold.library.public import Safeguard, record_replica
from shelfhold.library.tombstones import TombstoneRoot, TombstoneService
from tests.export_env import ExportEnv, Series
from tests.fakes_clock import ManualClock
from tests.intents_env import NOW
from tests.library_env import LibEnv
from tests.locks_env import index_lock

TITLE = "Dr Stone"
ADMIN = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)
KEY = Principal(
    "01J00000000000000000000ADM",
    "admin",
    "admin",
    Credential.API_KEY,
    key_id="01J0000000000000000000KEY0",
)
STALE = Principal(
    "01J00000000000000000000ADM", "admin", "admin", Credential.SESSION,
    session_id="01J0000000000000000000SES0", auth_time=datetime(2026, 11, 5, 6, 0, tzinfo=UTC),
)  # fmt: skip
READER = Principal("01J00000000000000000000USR", "reader", "user", Credential.LOCAL_CLI)
THUMBS = "cache/thumbnails"
CONTENT_CACHE = "cache/content"


@dataclass
class PurgeEnv:
    lib: LibEnv
    tombstones: TombstoneService
    purge: PurgeService
    bus: EventBus
    clock: ManualClock
    store: KeyringStore

    @property
    def fsops(self) -> Fsops:
        return self.lib.fsops

    @property
    def roots(self) -> list[TombstoneRoot]:
        return [
            TombstoneRoot(self.lib.library_root, str(self.lib.library)),
            TombstoneRoot(self.lib.store_root, str(self.lib.store)),
        ]

    def replay(self) -> None:
        self.tombstones.replay(self.roots)

    def count(self, table: sa.Table, *where: sa.ColumnElement[bool]) -> int:
        with self.lib.engine.connect() as connection:
            return int(
                connection.execute(
                    sa.select(sa.func.count()).select_from(table).where(*where)
                ).scalar_one()
            )

    def journal(self, root: Path) -> bytes:
        path = root / ".shelfhold" / "tombstones"
        return path.read_bytes() if path.exists() else b""

    def dump(self) -> str:
        """Every row of the tables a title could leak into, as text."""
        with self.lib.engine.connect() as connection:
            parts: list[str] = []
            for name in (
                "audit",
                "event",
                "content_tombstone",
                "tombstone_pending_root",
                "fs_intent",
            ):
                rows = connection.execute(sa.text(f"SELECT * FROM {name}")).all()  # noqa: S608
                parts.append(json.dumps([[str(c) for c in row] for row in rows]))
        return "\n".join(parts)


def make_purge_env(lib: LibEnv) -> PurgeEnv:
    with lib.engine.begin() as connection:
        if connection.execute(sa.select(platform.instance.c.instance_id)).first() is None:
            connection.execute(
                sa.insert(platform.instance).values(instance_id=new_ulid(), jurisdiction="FR")
            )
    keys = lib.tmp / "keys"
    keys.mkdir(exist_ok=True)
    store = KeyringStore(keys / "keyring", lock_path=keys / "keyring.lock")
    if not (keys / "keyring").exists():
        store.create(Keyring.generate(NOW))
    clock = ManualClock(NOW)
    bus = EventBus(clock)
    tombstones = TombstoneService(lib.engine, store.load(), lib.fsops, bus)
    purge = PurgeService(
        lib.engine,
        lib.fsops,
        tombstones,
        clock=clock,
        data_dir=lib.data,
        bus=bus,
        fresh_auth_s=900,
        index_lock=index_lock(lib.engine),
    )
    env = PurgeEnv(lib, tombstones, purge, bus, clock, store)
    env.replay()  # membership fails closed until a replay ran
    return env


def spread(env: ExportEnv, series: Series, number: str = "1") -> tuple[str, dict[str, object]]:
    """One Unit with a copy everywhere: received in /library, exported, safeguarded in /store, in
    the bin of the library root, in the thumbnail and content caches, with an upstream claim and an
    open download ticket. Returns the Unit and what was placed (for the assertions)."""
    lib = env.lib
    unit = env.add_chapter(series, number)
    Safeguard(lib.engine, lib.fsops, lib.locate, str(lib.store), lib.data).run(unit)
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    binned = lib.manga / "Old" / f"{unit}.cbz"
    lib.fsops.write_atomic(str(binned), b"PK-binned-copy", parents=True)
    moved = lib.fsops.recycle(str(binned))
    recycle = Path(moved).relative_to(lib.library / ".shelfhold" / "recycle")
    with lib.engine.begin() as connection:
        connection.execute(
            sa.insert(storage.trash_item).values(
                id=new_ulid(), kind="unit", ref=unit, path=recycle.as_posix(),
                physical_root_id=lib.library_root, deleted_at=NOW, purge_after=NOW,
            )
        )  # fmt: skip
        hashes = connection.execute(
            sa.select(storage.content.c.content_hash).where(storage.content.c.unit_id == unit)
        ).scalar_one()
        record_replica(
            connection, unit_id=unit, location="upstream", content_hash=hashes,
            independent=True, remote_ref=f"suwayomi:chapter/{unit}", verified_at=NOW,
        )  # fmt: skip
    caches = []
    for base in (THUMBS, CONTENT_CACHE):
        folder = lib.data / base / unit[:2] / unit
        folder.mkdir(parents=True)
        (folder / "0.webp").write_bytes(b"RIFF-thumbnail")
        caches.append(folder)
    return unit, {"binned": Path(moved), "caches": caches}


def open_ticket(env: ExportEnv, unit: str, state: str = "requested") -> str:
    """A download ticket of the Unit's Variant, as the follow cycle would have opened it."""
    with env.engine.begin() as connection:
        variant, binding = connection.execute(
            sa.text("SELECT id, source_binding_id FROM unit_variant WHERE unit_id = :u"),
            {"u": unit},
        ).one()
        request = new_ulid()
        connection.execute(
            sa.insert(acquisition.download_request).values(
                id=request, target_kind="unit", variant_id=variant, source_binding_id=binding,
                state=state, ticket="suwayomi-ticket-1",
            )
        )  # fmt: skip
    return request
