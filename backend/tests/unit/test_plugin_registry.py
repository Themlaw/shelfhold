"""Plugin host registry: api N / N-1 shim, N-2 refused, first-party only in M1a."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.plugin_host.registry import (
    PluginCandidate,
    PluginOrigin,
    PluginRegistry,
    PluginStatus,
    candidates_from_entry_points,
    negotiate_api,
)
from shelfhold.sdk.models import API_VERSION
from shelfhold.sdk.testing.fakes import FakeRangedConnectedService


def _raw(key: str, api: int, **extra: Any) -> dict[str, Any]:
    manifest = json.loads(FakeRangedConnectedService().manifest.model_dump_json())
    return {**manifest, "key": key, "api": api, **extra}


def _plugin(raw: dict[str, Any]) -> Any:
    return SimpleNamespace(manifest=raw)


def _candidate(
    key: str, api: int, origin: PluginOrigin = PluginOrigin.FIRST_PARTY
) -> PluginCandidate:
    return PluginCandidate(key=key, origin=origin, load=lambda: _plugin(_raw(key, api)))


@pytest.mark.req("EF-SRC-001")
def test_source_contract_api_n_minus_1_shim() -> None:
    # real contract: N loads natively, N-1 through the shim, N-2 and the future are refused
    assert negotiate_api(API_VERSION).status is PluginStatus.LOADED
    assert negotiate_api(API_VERSION - 1).status is PluginStatus.LOADED_SHIM
    for declared in (API_VERSION - 2, API_VERSION + 1):
        verdict = negotiate_api(declared)
        assert verdict.status is PluginStatus.INCOMPATIBLE
        assert str(declared) in (verdict.reason or "")

    registry = PluginRegistry()
    records = registry.load_all(
        [
            _candidate("n", API_VERSION),
            _candidate("n-1", API_VERSION - 1),
            _candidate("n-2", API_VERSION - 2),
            _candidate("last", API_VERSION),
        ]
    )
    by_key = {r.key: r for r in records}
    assert by_key["n"].status is PluginStatus.LOADED
    assert by_key["n-1"].status is PluginStatus.LOADED_SHIM
    assert by_key["n-1"].manifest is not None and by_key["n-1"].manifest.key == "n-1"
    assert by_key["n-2"].status is PluginStatus.INCOMPATIBLE
    assert by_key["n-2"].reason and "incompatible" in by_key["n-2"].reason
    assert by_key["last"].status is PluginStatus.LOADED  # the others still load


@pytest.mark.req("EF-SRC-001")
def test_shim_translates_the_previous_manifest_shape() -> None:
    def shim(raw: dict[str, Any]) -> dict[str, Any]:
        migrated = {k: v for k, v in raw.items() if k != "lang"}
        migrated["langs"] = [raw["lang"]]
        migrated["api"] = 3
        return migrated

    registry = PluginRegistry(api_version=3, shims={2: shim})
    old = _raw("old", 2)
    old.pop("langs")
    old["lang"] = "fr"
    record = registry.load_all(
        [PluginCandidate(key="old", origin=PluginOrigin.FIRST_PARTY, load=lambda: _plugin(old))]
    )[0]
    assert record.status is PluginStatus.LOADED_SHIM
    assert record.manifest is not None and record.manifest.langs == ["fr"]


@pytest.mark.req("ENF-SEC-009")
def test_only_first_party_plugins_loaded_third_party_disabled() -> None:
    def forbidden() -> Any:
        raise AssertionError("third-party plugin code must never be loaded in M1a")

    registry = PluginRegistry()
    records = registry.load_all(
        [
            _candidate("native", API_VERSION),
            PluginCandidate(key="outsider", origin=PluginOrigin.THIRD_PARTY, load=forbidden),
        ]
    )
    by_key = {r.key: r for r in records}
    assert by_key["native"].status is PluginStatus.LOADED and by_key["native"].instance is not None
    assert by_key["outsider"].status is PluginStatus.DISABLED
    assert by_key["outsider"].instance is None and by_key["outsider"].manifest is None
    assert [r.key for r in registry.active()] == ["native"]


@pytest.mark.req("ENF-SEC-009")
def test_entry_point_origin_comes_from_the_distribution_never_from_loading() -> None:
    loaded: list[str] = []

    def entry_point(name: str, dist: str) -> Any:
        def load() -> Any:
            loaded.append(name)
            return _plugin(_raw(name, API_VERSION))

        return SimpleNamespace(name=name, dist=SimpleNamespace(name=dist), load=load)

    nodist = SimpleNamespace(name="nodist", dist=None, load=lambda: loaded.append("nodist"))
    eps = [entry_point("mine", "shelfhold"), entry_point("theirs", "shelfhold-evil"), nodist]
    candidates = candidates_from_entry_points(eps)
    assert {c.key: c.origin for c in candidates} == {
        "mine": PluginOrigin.FIRST_PARTY,
        "theirs": PluginOrigin.THIRD_PARTY,
        "nodist": PluginOrigin.THIRD_PARTY,
    }
    assert loaded == []  # classification imported nothing
    PluginRegistry().load_all(candidates)
    assert loaded == ["mine"]


@pytest.mark.req("ENF-SEC-009")
def test_third_party_stays_disabled_even_when_listed_active() -> None:
    registry = PluginRegistry(active_keys=("outsider",))
    record = registry.load_all(
        [PluginCandidate(key="outsider", origin=PluginOrigin.THIRD_PARTY, load=lambda: 1 / 0)]
    )[0]
    assert record.status is PluginStatus.DISABLED
    assert record.reason and "third" in record.reason


@pytest.mark.req("ENF-LEG-001")
def test_plugin_that_fails_to_load_is_reported_not_raised() -> None:
    def boom() -> Any:
        raise RuntimeError("broken")

    records = PluginRegistry().load_all(
        [
            PluginCandidate(key="bad", origin=PluginOrigin.FIRST_PARTY, load=boom),
            _candidate("good", API_VERSION),
        ]
    )
    by_key = {r.key: r for r in records}
    assert by_key["bad"].status is PluginStatus.INCOMPATIBLE
    assert by_key["good"].status is PluginStatus.LOADED


REPO = Path(__file__).resolve().parents[3]


@pytest.mark.req("EF-SRC-033", "EF-BIB-051")
def test_meta_suwayomi_manifest_is_valid_untrusted_and_inactive() -> None:
    from shelfhold.sdk.source import SourceManifest

    path = REPO / "sources" / "meta_suwayomi" / "manifest.json"
    manifest = SourceManifest.model_validate_json(path.read_text(encoding="utf-8"))
    assert manifest.key == "suwayomi"
    assert manifest.api == API_VERSION
    assert manifest.content_trust == "untrusted"  # fetches from the web
    assert "SUB_SOURCES" in manifest.model_dump(mode="json")["capabilities"]
    assert manifest.permissions.domains == []  # the instance is the user's, via lan_targets only


@pytest.mark.req("ENF-SEC-009")
def test_duplicate_plugin_key_is_refused() -> None:
    registry = PluginRegistry()
    first = registry.load_all([_candidate("same", API_VERSION)])[0]
    again = registry.load_all([_candidate("same", API_VERSION)])[0]
    assert first.status is PluginStatus.LOADED
    assert again.status is PluginStatus.INCOMPATIBLE and "duplicate" in (again.reason or "")


@pytest.mark.req("ENF-LEG-001")
def test_fresh_registry_switches_no_plugin_on() -> None:
    registry = PluginRegistry(active_keys=())
    registry.load_all([_candidate("native", API_VERSION)])
    assert registry.active() == []


def _counting(key: str, origin: PluginOrigin, calls: list[str], tag: str) -> PluginCandidate:
    def load() -> Any:
        calls.append(tag)
        return _plugin(_raw(key, API_VERSION))

    return PluginCandidate(key=key, origin=origin, load=load)


@pytest.mark.req("ENF-SEC-009")
@pytest.mark.parametrize("third_first", [True, False])
def test_third_party_homonym_never_shadows_the_first_party_plugin(third_first: bool) -> None:
    calls: list[str] = []
    mine = _counting("same", PluginOrigin.FIRST_PARTY, calls, "mine")
    theirs = _counting("same", PluginOrigin.THIRD_PARTY, calls, "theirs")
    registry = PluginRegistry()
    records = registry.load_all([theirs, mine] if third_first else [mine, theirs])
    assert calls == ["mine"]  # the homonym is never loaded
    by_origin = {r.origin: r for r in records}
    assert by_origin[PluginOrigin.FIRST_PARTY].status is PluginStatus.LOADED
    refused = by_origin[PluginOrigin.THIRD_PARTY]
    assert refused.status is PluginStatus.INCOMPATIBLE
    assert refused.reason and refused.reason.startswith("duplicate_key")
    assert registry.get("same") is by_origin[PluginOrigin.FIRST_PARTY]
    assert [r.origin for r in registry.active()] == [PluginOrigin.FIRST_PARTY]


@pytest.mark.req("ENF-SEC-009")
def test_two_first_party_plugins_with_one_key_are_all_refused_unloaded() -> None:
    calls: list[str] = []
    registry = PluginRegistry()
    records = registry.load_all(
        [
            _counting("same", PluginOrigin.FIRST_PARTY, calls, "a"),
            _counting("same", PluginOrigin.FIRST_PARTY, calls, "b"),
            _counting("other", PluginOrigin.FIRST_PARTY, calls, "c"),
        ]
    )
    assert calls == ["c"]  # neither homonym was loaded
    assert [r.status for r in records[:2]] == [PluginStatus.INCOMPATIBLE] * 2
    assert all("duplicate_key" in (r.reason or "") for r in records[:2])
    assert records[2].status is PluginStatus.LOADED
    assert registry.active() == [records[2]]


@pytest.mark.req("ENF-SEC-009")
def test_distribution_names_are_normalised_per_pep_503() -> None:
    def ep(name: str, dist: str) -> Any:
        return SimpleNamespace(name=name, dist=SimpleNamespace(name=dist), load=lambda: None)

    found = candidates_from_entry_points([ep("a", "Shelfhold"), ep("b", "shelf.hold")])
    assert [c.origin for c in found] == [PluginOrigin.FIRST_PARTY, PluginOrigin.THIRD_PARTY]
