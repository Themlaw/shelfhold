"""A synthetic instance for the tombstone tests (L33): two read-write roots with real sentinels,
a real database, a real keyring file, the real event bus. No mock of the OS, of SQLite or of the
cipher."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.events.bus import EventBus
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import coherence, platform
from shelfhold.infrastructure.secrets.keyring import Keyring, KeyringStore
from shelfhold.library.tombstones import (
    ContentHash,
    PlaceRef,
    SeriesBindingRef,
    TombstoneRoot,
    TombstoneService,
    VariantRef,
)
from shelfhold.library.tombstones.index import Normalizers
from tests.fakes_clock import ManualClock
from tests.fsops_env import Env, make_env
from tests.intents_env import NOW, prepare_database

TITLE = "The Secret Garden of Velvet Thorns"
SLUG = "the-secret-garden-of-velvet-thorns"
HASH = "sha256:" + "4f" * 32
VARIANT = VariantRef(
    "suwayomi", "4422", f"/chapter/{SLUG}-ch-7", (ContentHash("content-hash@1", HASH),)
)
PLACE = PlaceRef.of(
    {
        "work_id": "01J0000000000000000000WORK",
        "lang": "fr",
        "translation_type": "official",
        "media": "images",
    },
    track="main",
    number_segment="",
    number_raw="7",
    part="",
)
SERIES = SeriesBindingRef("suwayomi", "4422", f"/manga/{SLUG}")
JOURNAL = ".shelfhold/tombstones"
WORK = "01J0000000000000000000WORK"
# The reserved waiver of the lost keyring, from the local CLI as an admin.
ADMIN = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)


@dataclass
class TombEnv:
    tmp: Path
    fs: Env
    engine: Engine
    store: KeyringStore
    bus: EventBus
    roots: list[TombstoneRoot]
    services: list[TombstoneService] = field(default_factory=list)

    @property
    def library(self) -> TombstoneRoot:
        return self.roots[0]

    @property
    def books(self) -> TombstoneRoot:
        return self.roots[1]

    def service(
        self, keyring: Keyring | None = None, *, normalizers: Normalizers | None = None
    ) -> TombstoneService:
        made = TombstoneService(
            self.engine,
            keyring or self.store.load(),
            self.fs.fsops,
            self.bus,
            normalizers=normalizers,
        )
        self.services.append(made)
        return made

    def journal_path(self, root: TombstoneRoot) -> Path:
        return Path(root.path) / JOURNAL

    def journal(self, root: TombstoneRoot) -> bytes:
        path = self.journal_path(root)
        return path.read_bytes() if path.exists() else b""

    def sentinel(self, root: TombstoneRoot) -> dict[str, Any]:
        return json.loads((Path(root.path) / ".shelfhold-mount-id").read_text("utf-8"))

    def events(self, event_type: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(platform.event.c.payload)
                .where(platform.event.c.type == event_type)
                .order_by(platform.event.c.id)
            ).scalars()
            return [dict(row) for row in rows]

    def table_rows(self) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(coherence.content_tombstone).order_by(coherence.content_tombstone.c.seq)
            ).mappings()
            return [dict(row) for row in rows]

    def unmounted(self, root: TombstoneRoot) -> TombstoneRoot:
        return TombstoneRoot(root.root_id, root.path, mounted=False)


def make_tomb_env(tmp: Path, *, database: Path | None = None) -> TombEnv:
    fs = make_env(tmp)
    engine = prepare_database(database or tmp / "shelfhold.db")
    with write_transaction(engine) as connection:
        for root in fs.expected:
            connection.execute(
                sa.insert(platform.physical_root).values(root_id=root.root_id, path_entry=root.path)
            )
    data = tmp / "data"
    data.mkdir()
    store = KeyringStore(data / "keyring", lock_path=data / "keyring.lock")
    store.create(Keyring.generate(NOW))
    roots = [TombstoneRoot(root.root_id, root.path) for root in fs.expected]
    return TombEnv(tmp, fs, engine, store, EventBus(ManualClock(NOW)), roots)
