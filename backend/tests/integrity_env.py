"""A served chapter whose received archive can be damaged, for the integrity check (EF-BIB-012).

Built on the upgrade world: a real exported chapter, its received archive in /library, the
same-path upgrade service. Nothing here touches the network.
"""

import io
import zipfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa

from shelfhold.acquisition.application.reacquire import Reacquirer
from shelfhold.export.domain.physical_roots import RECEIVED_DIR
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from shelfhold.library.application.integrity import IntegrityChecker, Throttle
from shelfhold.library.public import PassthroughStore
from shelfhold.library.replica import library_copies
from tests.export_env import ExportEnv
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED
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

    def repair_copy(self, data: bytes) -> str:
        """The fresh bytes of the Unit, filed as one more copy of its content (the ticket's own
        path, `PassthroughStore.ingest(repair=True)`); the content id."""
        with self.env.engine.connect() as connection:
            variant, content = connection.execute(
                sa.text("SELECT variant_id, id FROM content WHERE unit_id = :u"),
                {"u": self.exported.unit_id},
            ).one()
        lib = self.env.lib
        PassthroughStore(
            self.env.engine, lib.fsops, lib.locate, lib.data, tombstones=NONE_PURGED,
            hold_work=work_lock(self.env.engine),
        ).ingest(
            unit_id=self.exported.unit_id, variant_id=variant, staged=lib.stage(data),
            dest=str(lib.manga / RECEIVED_DIR / f"repair-{new_ulid()}.cbz"), repair=True,
        )  # fmt: skip
        return str(content)


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
