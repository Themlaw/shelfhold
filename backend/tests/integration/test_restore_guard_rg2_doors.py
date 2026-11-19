"""EF-INT-026, EF-EXP-029, I39 (ADR-0044): the removal of a destination and the full detachment
write through a write session, so they meet the whole-instance guard and the lease.

They used to build their `Fsops` from `Composition.read_context()`: no session, no lease, no look
at the other roots. That `Fsops` is now a view that refuses every write, and `DetachOps` opens a
write session for what deletes. Real `DetachOps`, real instance (only Kavita is simulated).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.remove_destination import RemovalMode, removal_phrase
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.schema import export, platform
from shelfhold.ops.detach import DetachOps
from tests.l50_env import seeded
from tests.maintenance_env import principal

pytestmark = pytest.mark.req("EF-INT-026", "EF-EXP-029")

SENTINEL = ".shelfhold-mount-id"


def test_a_removal_that_takes_the_files_is_refused_while_another_root_is_ahead(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in ("1", "2"):
            env.add_chapter(series, number)
        env.export()
        files = env.exported_files()
        assert files
        comp = env.inst.comp
        with env.inst.engine.connect() as connection:
            roots = sorted(
                connection.execute(sa.select(platform.physical_root.c.path_entry)).scalars()
            )
        store = str(settings.store_dir)
        assert store in roots and len(roots) == 2  # the files live on the other root
        sentinel = Path(store) / SENTINEL  # the store comes back ahead of the database
        document = json.loads(sentinel.read_text("utf-8"))
        document["generation"] = int(document["generation"]) + 5
        sentinel.write_text(json.dumps(document), "utf-8")
        ops = DetachOps(comp, SimpleNamespace(), flagged_names=lambda _id: 0)  # type: ignore[arg-type]
        phrase = removal_phrase(env.destination_id, RemovalMode.DELETE_FILES)
        with pytest.raises(RootBlockedError):
            ops.remove(
                destination_id=env.destination_id, mode=RemovalMode.DELETE_FILES.value,
                typed=phrase, give_up=False, principal=principal("admin", comp.clock.now()),
            )  # fmt: skip
        assert env.exported_files() == files  # nothing left the disk
        with env.inst.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count()).select_from(export.shelf_destination)
            ).scalar_one()
        assert count == 1  # and the destination was not forgotten
