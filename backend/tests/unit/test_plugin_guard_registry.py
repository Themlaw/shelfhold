"""EF-SRC-019 / ENF-SEC-009 (M1b L07): the registry's side of the guard, without the CLI."""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.plugin_host.registry import (
    InstalledPlugin,
    PluginCandidate,
    PluginOrigin,
    PluginRegistry,
    PluginStatus,
    third_party_candidates,
)
from shelfhold.sdk.models import API_VERSION
from tests.plugin_guard_env import manifest

pytestmark = pytest.mark.req("ENF-SEC-009")


def _candidate(key: str, origin: PluginOrigin, dist: str | None = None) -> PluginCandidate:
    plugin = SimpleNamespace(manifest={**manifest(key), "api": API_VERSION})
    return PluginCandidate(key=key, origin=origin, load=lambda: plugin, dist=dist)


def test_conflicting_key_is_refused_after_pep_503_normalisation() -> None:
    registry = PluginRegistry(granted_distributions=["theirs"])
    mine = _candidate("my-source", PluginOrigin.FIRST_PARTY)
    records = registry.load_all([mine, _candidate("My_Source", PluginOrigin.THIRD_PARTY, "theirs")])
    assert [r.status for r in records] == [PluginStatus.LOADED, PluginStatus.INCOMPATIBLE]
    assert "duplicate_key" in (records[1].reason or "")


def test_two_granted_third_party_homonyms_both_refused() -> None:
    registry = PluginRegistry(granted_distributions=["a", "b"])
    records = registry.load_all(
        [
            _candidate("Same.Key", PluginOrigin.THIRD_PARTY, "a"),
            _candidate("same-key", PluginOrigin.THIRD_PARTY, "b"),
        ]
    )
    assert {r.status for r in records} == {PluginStatus.INCOMPATIBLE}


def test_granted_third_party_loads_only_for_its_own_distribution() -> None:
    registry = PluginRegistry(granted_distributions=["Granted_Dist"])
    ok, other, blocked = registry.load_all(
        [
            _candidate("one", PluginOrigin.THIRD_PARTY, "granted-dist"),
            _candidate("two", PluginOrigin.THIRD_PARTY, "not-granted"),
            PluginCandidate(
                "three", PluginOrigin.THIRD_PARTY, lambda: 1 / 0, dist="granted-dist", blocked="no"
            ),
        ]
    )
    assert ok.status is PluginStatus.LOADED
    assert other.status is PluginStatus.DISABLED and blocked.status is PluginStatus.DISABLED
    assert blocked.reason == "no"


def test_discovery_reads_the_third_party_directory_only_and_imports_nothing(tmp_path: Path) -> None:
    for name, ep in (("good-1.0", "good"), ("shelfhold-9.9", "evil"), ("stray-1.0", "stray")):
        info = tmp_path / f"{name}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name.split('-')[0]}\nVersion: 1\n"
        )
        (info / "entry_points.txt").write_text(f"[shelfhold.plugins]\n{ep} = {ep}_mod:plugin\n")
    before = set(sys.modules)
    found = third_party_candidates(tmp_path, [InstalledPlugin("good", ("good_mod",))], blocked={})
    assert [(c.key, c.dist, c.origin) for c in found] == [
        ("good", "good", PluginOrigin.THIRD_PARTY)
    ]
    assert set(sys.modules) == before
    assert str(tmp_path) not in sys.path


def test_entry_point_outside_the_plugin_modules_is_blocked(tmp_path: Path) -> None:
    info = tmp_path / "p-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: p\nVersion: 1\n")
    (info / "entry_points.txt").write_text("[shelfhold.plugins]\nx = os:getcwd\n")
    (found,) = third_party_candidates(tmp_path, [InstalledPlugin("p", ("p_mod",))], blocked={})
    assert found.blocked and "outside" in found.blocked
    record = PluginRegistry(granted_distributions=["p"]).load_all([found])[0]
    assert record.status is PluginStatus.DISABLED


def test_loader_serves_granted_tops_from_the_end_of_meta_path_never_sys_path(
    tmp_path: Path,
) -> None:
    from shelfhold.plugin_host import registry

    marker = tmp_path / "ran"
    (tmp_path / "x.pth").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n")
    (tmp_path / "granted_mod").mkdir()
    (tmp_path / "granted_mod" / "__init__.py").write_text("VALUE = 1\n")
    (tmp_path / "other_mod.py").write_text("VALUE = 2\n")
    ep: Any = SimpleNamespace(load=lambda: object(), module="m", name="k")
    paths, metas = list(sys.path), list(sys.meta_path)
    try:
        registry._third_party_loader(tmp_path, ep, ["granted_mod"])()
        assert sys.path == paths and str(tmp_path) not in sys.path
        assert isinstance(sys.meta_path[-1], registry.ThirdPartyFinder)
        assert sys.meta_path[:-1] == metas
        import importlib

        assert importlib.import_module("granted_mod").VALUE == 1
        with pytest.raises(ImportError):
            importlib.import_module("other_mod")  # in the directory, not granted
        assert not (tmp_path / "granted_mod" / "__pycache__").exists()  # no bytecode written
    finally:
        sys.path[:] = paths
        sys.meta_path[:] = metas
        sys.modules.pop("granted_mod", None)
    assert not marker.exists()
