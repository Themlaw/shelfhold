"""A served chapter whose received archive can be damaged, for the integrity check (EF-BIB-012).

Built on the upgrade world: a real exported chapter, its received archive in /library, the
same-path upgrade service. Nothing here touches the network.
"""

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa

from shelfhold.acquisition.application.reacquire import Reacquirer
from shelfhold.export.application.library_repair import LibraryRepair
from shelfhold.infrastructure.schema import platform
from shelfhold.library.application.integrity import IntegrityChecker, Throttle
from shelfhold.library.replica import library_copies
from tests.export_env import ExportEnv
from tests.locks_env import work_lock
from tests.upgrade_env import Exported, service


@dataclass
class Rig:
    env: ExportEnv
    exported: Exported
    sleeps: list[float] = field(default_factory=list)
    accessed: list[int] = field(default_factory=list)  # one entry per window-guard call

    def checker(self, *, rate: int | None = None) -> IntegrityChecker:
        return IntegrityChecker(
            self.env.engine,
            _bus(self.env),
            self.env.clock,
            throttle=Throttle(rate, clock=lambda: 0.0, sleep=self.sleeps.append),
            guard=lambda: self.accessed.append(1),
            recheck_after=timedelta(days=30),
        )

    def reacquirer(self) -> Reacquirer:
        from shelfhold.library.public import irreplaceability

        return Reacquirer(
            self.env.engine,
            _bus(self.env),
            self.env.clock,
            irreplaceable=lambda c, unit: irreplaceability(c, unit).irreplaceable,
        )

    def received(self) -> Path:
        with self.env.engine.connect() as connection:
            copies = library_copies(connection, self.exported.unit_id)
        return Path(copies[0].local)

    def state(self) -> tuple[str, object]:
        with self.env.engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT integrity_state, integrity_checked_at FROM unit WHERE id = :u"),
                {"u": self.exported.unit_id},
            ).one()
        return str(row[0]), row[1]

    def events(self, event_type: str) -> list[sa.Row[tuple[object, ...]]]:
        with self.env.engine.connect() as connection:
            return list(
                connection.execute(
                    sa.select(platform.event).where(platform.event.c.type == event_type)
                )
            )

    def requests(self) -> list[sa.Row[tuple[object, ...]]]:
        with self.env.engine.connect() as connection:
            return list(
                connection.execute(
                    sa.text(
                        "SELECT * FROM download_request WHERE unit_id = :u ORDER BY requested_at"
                    ),
                    {"u": self.exported.unit_id},
                )
            )

    def upgrader(self) -> object:
        return service(self.env)

    def repair_copy(self, data: bytes) -> bool:
        """The fresh bytes of the Unit, put in the place of the damaged library file by the repair
        the ticket calls (`LibraryRepair`); True when a file was replaced."""
        return repair_with(self.env.engine, self.env.lib, self.env.clock, _bus(self.env),
                           self.exported.unit_id, data)  # fmt: skip


def repair_with(engine, lib, clock, bus, unit_id: str, data: bytes) -> bool:  # type: ignore[no-untyped-def]
    with engine.connect() as connection:
        variant = connection.execute(
            sa.text("SELECT variant_id FROM content WHERE unit_id = :u"), {"u": unit_id}
        ).scalar_one()
    staged = lib.stage(data)
    return LibraryRepair(
        engine,
        lib.fsops,
        lib.locate,
        clock,
        hold_work=work_lock(engine),
        bus=bus,
        data_dir=lib.data,
    )(unit_id, variant, staged, "sha256:" + hashlib.sha256(data).hexdigest())


def held_repair(env: ExportEnv) -> LibraryRepair:
    return LibraryRepair(
        env.engine, env.lib.fsops, env.lib.locate, env.clock, hold_work=work_lock(env.engine),
        bus=_bus(env), data_dir=env.lib.data,
    )  # fmt: skip


def _bus(env: ExportEnv) -> object:
    from shelfhold.events.bus import EventBus

    return EventBus(env.clock)


def flip_page_byte(path: Path) -> None:
    """Damage one stored page: the entry keeps its header and its CRC, the bytes no longer fit."""
    data = bytearray(path.read_bytes())
    at = data.index(b"\x89PNG") + 12
    data[at] ^= 0xFF
    path.write_bytes(bytes(data))


def zip_with(entries: list[tuple[str, bytes]], *, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
    return buffer.getvalue()
