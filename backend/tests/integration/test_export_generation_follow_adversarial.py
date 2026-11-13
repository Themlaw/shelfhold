"""Review of #241: the generation follow must never lift a freeze the database holds.

The export opens its `Fsops` with a snapshot of the freeze (`loaded.freeze.fork()`), read when it
opened. While it waits for its scan, another task moves the generation, then another writer finds
the database behind and persists the instance freeze (`instance.restore_generation`, EF-INT-026).
Before #241 the sentinel ahead of the held snapshot refused the scan (`restored_behind`). With the
follow, `Fsops` reads the generation again, finds it equal and lets the scan through although the
database says `restored_behind`: no write, deletion or scan (ADR-0044)."""

from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.fsops import RootBlockedError
from tests.l41_env import instance


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_follow_never_lifts_a_freeze_the_database_holds(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        with inst.comp.write_context():
            pass  # the root has a sentinel and a generation
        fsops, _loaded, _tombstones = inst.comp.read_context()  # the export opens its Fsops
        fsops.require_scan_allowed(str(inst.library))
        with inst.comp.write_context():
            pass  # another task moves the generation while the export waits
        with inst.engine.begin() as connection:
            FreezeState().persist(connection, 1)  # another writer froze the instance
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))  # followed: the scan goes anyway
        assert caught.value.state is RootState.RESTORED_BEHIND
