"""A small instance for the backup tests (M1b L23): a migrated database, a keyring, some meta
files, a passphrase file outside `/data`, a backup directory outside `/data` and `/store`. The
clock is moved by hand (an archive is named after the second it was made)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import platform
from shelfhold.infrastructure.secrets import diagnostics
from shelfhold.jobs.maintenance import MaintenanceGate
from shelfhold.ops.backup import BackupService
from tests.fakes_clock import ManualClock

PASSPHRASE = "synthetic backup passphrase 12345"  # noqa: S105
MARKER = "marker-value-not-for-the-disk-9f3a"  # a string that must never be readable in a backup
START = datetime(2026, 11, 8, 1, 40, tzinfo=UTC)


@dataclass
class BackupBench:
    root: Path
    settings: Settings
    engine: sa.Engine
    clock: ManualClock
    bus: EventBus
    gate: MaintenanceGate
    service: BackupService
    passphrase: Path

    @property
    def backups(self) -> Path:
        assert self.settings.backup_dir is not None
        return self.settings.backup_dir

    def names(self) -> list[str]:
        return sorted(p.name for p in self.backups.glob("shelfhold-backup-*.tar.age"))

    def events(self, event_type: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(platform.event.c.payload).where(platform.event.c.type == event_type)
            )
            return [dict(r.payload) for r in rows]

    def service_for(self, settings: Settings, **kw: Any) -> BackupService:
        return BackupService(
            settings, self.engine, clock=self.clock, bus=self.bus, gate=self.gate, **kw
        )


def write_passphrase(path: Path, text: str = PASSPHRASE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@contextmanager
def backup_bench(root: Path, **overrides: Any) -> Iterator[BackupBench]:
    data, store = root / "data", root / "store"
    base: dict[str, Any] = {
        "data_dir": data,
        "store_dir": store,
        "backup_dir": root / "backups",
        "backup_passphrase_file": write_passphrase(root / "secrets" / "backup-passphrase"),
    }
    settings = Settings(**{**base, **overrides})
    migrate(settings)
    diagnostics.boot_check(settings, announce=False)  # a keyring in /data
    (data / "meta" / "units" / "ab").mkdir(parents=True)
    (data / "meta" / "units" / "ab" / "01JTEST0000000000000000AB1.json").write_text(
        '{"unit": "a"}', encoding="utf-8"
    )
    engine = create_db_engine(settings.database_path)
    with engine.begin() as connection:
        connection.execute(sa.insert(platform.app_setting).values(key="test.marker", value=MARKER))
    clock = ManualClock(START)
    bus = EventBus(clock)
    gate = MaintenanceGate()
    service = BackupService(settings, engine, clock=clock, bus=bus, gate=gate)
    try:
        yield BackupBench(
            root, settings, engine, clock, bus, gate, service, settings.backup_passphrase_file
        )  # type: ignore[arg-type]
    finally:
        engine.dispose()
