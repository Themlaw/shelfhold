"""A root that comes back (remount, L25 exit) is replayed before the index answers: a purge
recorded in its journal is not forgotten until the next restart (EF-BIB-054)."""

import os
import shutil
from pathlib import Path

import pytest

from shelfhold.export.domain.physical_roots import RootState
from shelfhold.library.tombstones import TombstoneRoot
from tests.l41_env import instance
from tests.tombstone_env import VARIANT

pytestmark = pytest.mark.req("EF-BIB-054")


def test_root_that_comes_back_is_replayed_without_a_restart(settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        with inst.comp.write_context():  # stamps the sentinel of the library root
            pass
        fsops, loaded, service = inst.comp.read_context()
        root = next(iter(loaded.roots))
        assert fsops.root_state(root.path) is RootState.OK
        assert not service.check(VARIANT, root_id=root.root_id).blocked

        inst.engine.dispose()  # the backup taken before the purge
        snapshot = tmp_path / "before-purge.db"
        shutil.copy(settings.database_path, snapshot)

        recorded = service.record(
            VARIANT, [TombstoneRoot(root.root_id, root.path, mounted=True)], reason="purge_unit"
        )
        assert recorded.written == (root.root_id,)
        assert service.check(VARIANT, root_id=root.root_id).blocked

        inst.engine.dispose()  # the database is put back; the root is away
        shutil.copy(snapshot, settings.database_path)
        inst.comp._tombstones = None  # a new process
        library_root = Path(root.path)
        away = tmp_path / "away"
        os.rename(library_root, away)
        library_root.mkdir()  # the mount point, empty: root_lost

        fsops2, _, service2 = inst.comp.read_context()
        assert fsops2.root_state(root.path) is not RootState.OK
        inst.comp.root_watcher().refresh()
        assert not service2.check(VARIANT, root_id=root.root_id).blocked  # not mounted: unknown

        library_root.rmdir()  # it comes back: `root_lost` lifts itself
        os.rename(away, library_root)
        inst.comp.root_watcher().refresh()
        fsops3, _, service3 = inst.comp.read_context()
        assert fsops3.root_state(root.path) is RootState.OK
        assert service3.check(VARIANT, root_id=root.root_id).blocked


def test_lines_sealed_under_a_predecessor_root_are_indexed_at_replay(
    settings, tmp_path: Path
) -> None:
    """After a takeover the root keeps its journal, sealed under the identity of its predecessor:
    the replay the next call runs opens those lines (not only the flush of the owed ones)."""
    import sqlalchemy as sa

    from shelfhold.infrastructure.ids import new_ulid
    from shelfhold.infrastructure.schema import coherence, platform

    with instance(settings, tmp_path) as inst:
        inst.configure()
        with inst.comp.write_context():
            pass
        _, loaded, service = inst.comp.read_context()
        root = next(iter(loaded.roots))
        old = new_ulid()  # the predecessor's identity, under which the line was sealed
        with inst.engine.begin() as connection:  # a row the pending table can refer to, for now
            connection.execute(
                sa.insert(platform.physical_root).values(root_id=old, path_entry="/elsewhere")
            )
        service.record(VARIANT, [TombstoneRoot(old, root.path, mounted=True)], reason="purge_unit")
        with inst.engine.begin() as connection:  # only the journal remembers the purge
            connection.execute(
                sa.delete(platform.physical_root).where(platform.physical_root.c.root_id == old)
            )
            connection.execute(sa.delete(coherence.content_tombstone))
            connection.execute(
                sa.update(platform.physical_root)
                .where(platform.physical_root.c.root_id == root.root_id)
                .values(predecessor_instance_ids=[old])
            )
        inst.comp._tombstones = None
        _, _, again = inst.comp.read_context()
        assert not again.report.inert  # the line opens under the predecessor's identity
        assert again.report.verified == 1
        verdict = again.check(VARIANT, root_id=root.root_id)
        assert verdict.blocked and verdict.cause != "journal_truncated"
