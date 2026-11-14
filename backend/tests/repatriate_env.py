"""An adopted Kavita library whose files live in a volume outside `/library`, then mounted
read-only in Shelfhold (`adopt_ro`): the situation before the switchover from Suwayomi's own
download folder (EF-EXP-051, EF-EXP-060). Real files, real database, the simulated Kavita.

The library is adopted in place, then its tree is moved to the upstream volume and an empty
writable root takes its place: the files are where an old automation put them.
"""

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from shelfhold.export.application.repatriate import Repatriator
from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMode
from shelfhold.export.domain.physical_roots import ExpectedRoot, SentinelDocument
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity, platform, reading
from tests.adopt_env import Adoption, adoption, comicinfo, tree_state
from tests.export_env import ROOT_SUBPATH, ExportEnv, export_env
from tests.integration.test_schema_platform import ARGON
from tests.intents_env import NOW
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT

SERIES = ("Series Alpha", "Series Beta")
CHAPTERS = 3


@dataclass
class Repatriation:
    a: Adoption
    ro: Path  # the upstream volume, mounted read-only
    fsops: Fsops
    repatriator: Repatriator
    user_id: str

    @property
    def env(self) -> ExportEnv:
        return self.a.env

    @property
    def destination(self) -> str:
        return self.env.destination_id

    @property
    def upstream(self) -> Path:
        return self.ro / ROOT_SUBPATH

    def dry_run(self) -> object:
        return self.repatriator.plan(self.destination)

    def apply(self, fingerprint: str | None = None, **extra: object) -> object:
        return self.repatriator.apply(
            self.destination,
            fingerprint=fingerprint,
            job_id=new_ulid(),
            **extra,  # type: ignore[arg-type]
        )

    def confirmed(self) -> object:
        plan = self.repatriator.plan(self.destination)
        return self.apply(plan.fingerprint)

    def unmount(self) -> None:
        """What the admin does last: the old mount goes away."""
        shutil.rmtree(self.ro)

    def rows(self, sql: str, **params: object) -> list[dict[str, object]]:
        with self.env.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def upstream_state(self) -> dict[str, tuple[str, int, int, int]]:
        return tree_state(self.upstream)


def chapter_names(series: str) -> list[str]:
    return [f"{series} - Ch.{n}.cbz" for n in range(1, CHAPTERS + 1)]


@contextmanager
def repatriation_env(*, library_generation: int = 1) -> Iterator[Repatriation]:
    with export_env() as env:
        a = adoption(env)
        for name in SERIES:
            a.add_series(name, chapter_names(name), info=comicinfo(name, language="en"))
        a.adopt()
        user = _reader(env)
        ro = env.lib.tmp / "upstream-ro"
        ro.mkdir()
        shutil.move(str(env.root), str(ro / ROOT_SUBPATH))
        env.root.mkdir()
        fsops = _fsops_with(env, ro, library_generation)
        repatriator = Repatriator(
            env.engine, fsops, locate=env.lib.locate, clock=env.clock,
            free_bytes=lambda path: 10 << 30,
        )  # fmt: skip
        yield Repatriation(a, ro, fsops, repatriator, user)


def _reader(env: ExportEnv) -> str:
    """A reader with a position on every adopted file: Shelfhold's own progress (found by
    `export_item` and `content_hash`)."""
    user_id = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(identity.user).values(
                id=user_id, username="reader", password_hash=ARGON, role="admin", can_rate=True
            )
        )
        for row in connection.execute(sa.select(export.export_record)).all():
            connection.execute(
                sa.insert(reading.reading_position).values(
                    id=new_ulid(), user_id=user_id, carrier_type="export_item",
                    carrier_id=row.export_item_id, content_hash=row.file_hash,
                    locator={"page": 3}, percent=0.5, client_ts=NOW, server_ts=NOW,
                )
            )  # fmt: skip
    return user_id


def _fsops_with(env: ExportEnv, ro: Path, generation: int) -> Fsops:
    """The same roots as the library, plus the upstream volume as an `adopt_ro` entry (also in
    the path map of the target, where the pipeline looks for it)."""
    lib = env.lib
    sentinel = SentinelDocument.parse((lib.library / ".shelfhold-mount-id").read_bytes())
    expected = [
        ExpectedRoot(lib.library_root, str(lib.library), sentinel.instance_id, generation),
        ExpectedRoot(lib.store_root, str(lib.store), sentinel.instance_id, 1),
    ]
    entries = (
        PathEntry(str(lib.manga), "/manga"),
        PathEntry(str(lib.store), "/store"),
        PathEntry(str(ro), "/upstream/suwayomi", PathMode.ADOPT_RO),
    )
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.update(export.export_target)
            .where(export.export_target.c.id == env.target_id)
            .values(
                path_map=[
                    {"local": str(lib.manga), "remote": "/manga", "mode": "rw"},
                    {"local": str(ro), "remote": "/upstream/suwayomi", "mode": "adopt_ro"},
                ]
            )
        )
        connection.execute(
            sa.insert(platform.physical_root).values(
                root_id=new_ulid(), path_entry=str(ro), mode="adopt_ro"
            )
        )
    return Fsops.open(PathMap(entries), expected)


def kavita_remote(series: str, name: str) -> str:
    return f"{REMOTE_ROOT}/{series}/{name}"


__all__ = ["LIBRARY_ID", "REMOTE_ROOT", "Repatriation", "chapter_names", "repatriation_env"]
