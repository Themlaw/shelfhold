"""The roots the replay found away or unreadable are looked at again, at a bounded cost: a root
that is there is never read again, a missing one costs a lookup, the others one every interval."""

import os
import shutil
from pathlib import Path

import pytest

from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.fsops import facade
from shelfhold.library.tombstones import TombstoneRoot
from tests.l41_env import instance
from tests.tombstone_env import VARIANT

pytestmark = pytest.mark.req("EF-BIB-054", "ENF-OPS-014")


def _purged_db_restored(inst, settings, tmp_path: Path):  # type: ignore[no-untyped-def]
    inst.configure()
    with inst.comp.write_context():
        pass
    _, loaded, service = inst.comp.read_context()
    root = next(iter(loaded.roots))
    inst.engine.dispose()
    snapshot = tmp_path / "before-purge.db"
    shutil.copy(settings.database_path, snapshot)
    service.record(
        VARIANT, [TombstoneRoot(root.root_id, root.path, mounted=True)], reason="purge_unit"
    )
    inst.engine.dispose()
    shutil.copy(snapshot, settings.database_path)
    inst.comp._tombstones = None  # restart on the restored database
    inst.comp._watcher = None
    return root


def _flaky_once(monkeypatch: pytest.MonkeyPatch) -> None:
    real = facade.Fsops.read_tombstone_journal
    calls = {"n": 0}

    def flaky(self, root_path, *, max_bytes):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "EIO")  # a disk that is still spinning up
        return real(self, root_path, max_bytes=max_bytes)

    monkeypatch.setattr(facade.Fsops, "read_tombstone_journal", flaky)


def test_mounted_root_with_an_unreadable_journal_is_replayed_again(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with instance(settings, tmp_path) as inst:
        root = _purged_db_restored(inst, settings, tmp_path)
        inst.comp.probe_interval_s = 0.0  # the retry does not wait for the interval here
        _flaky_once(monkeypatch)
        fsops, _, first = inst.comp.read_context()
        assert fsops.root_state(root.path) is RootState.OK
        assert not first.check(VARIANT, root_id=None).blocked  # fail closed on this root only
        _, _, later = inst.comp.read_context()
        assert later.check(VARIANT, root_id=None).blocked


def test_unreadable_root_is_probed_once_per_interval(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with instance(settings, tmp_path) as inst:
        _purged_db_restored(inst, settings, tmp_path)
        _flaky_once(monkeypatch)
        inst.comp.read_context()  # the replay that could not read the journal
        probes = {"n": 0}
        real = facade.Fsops.root_state

        def counted(self, path):  # type: ignore[no-untyped-def]
            probes["n"] += 1
            return real(self, path)

        monkeypatch.setattr(facade.Fsops, "root_state", counted)
        for _ in range(50):
            inst.comp.read_context()
        assert probes["n"] <= 2  # not one per call: the interval is 30 s


def test_missing_root_is_probed_at_every_call_and_a_present_one_never(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with instance(settings, tmp_path) as inst:
        root = _purged_db_restored(inst, settings, tmp_path)
        library = Path(root.path)
        os.rename(library, tmp_path / "away")
        library.mkdir()
        inst.comp.read_context()
        probes: list[str] = []
        real = facade.Fsops.root_state

        def counted(self, path):  # type: ignore[no-untyped-def]
            probes.append(path)
            return real(self, path)

        monkeypatch.setattr(facade.Fsops, "root_state", counted)
        for _ in range(5):
            inst.comp.read_context()
        assert probes.count(root.path) >= 5  # missing: looked at every call
        others = {p for p in probes if p != root.path}
        assert all(probes.count(p) <= 5 for p in others)
