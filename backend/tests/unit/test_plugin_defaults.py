"""ENF-LEG-001: fresh install ships zero plugin repos and no active source."""

from pathlib import Path

from shelfhold.config import ResolvedSettings, load_settings
from shelfhold.plugin_host.public import load_plugin_state


def _fresh(tmp_path: Path) -> ResolvedSettings:
    env = {
        "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
    }
    return load_settings(env)


def test_fresh_install_has_zero_plugin_repos(tmp_path: Path) -> None:
    resolved = _fresh(tmp_path)
    assert resolved.values.plugin_repos == ()
    assert resolved.fields["plugin_repos"].source == "default"
    assert load_plugin_state(resolved.values).plugin_repos == ()


def test_meta_sources_ship_with_no_active_source(tmp_path: Path) -> None:
    resolved = _fresh(tmp_path)
    assert resolved.values.active_sources == ()
    state = load_plugin_state(resolved.values)
    assert state.active_sources == ()
    assert state.active_meta_sources == ()
