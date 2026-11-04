"""ENF-LEG-001: fresh install ships zero plugin repos and no active source."""

from pathlib import Path

from shelfhold.plugin_host.public import load_plugin_state


def test_fresh_install_has_zero_plugin_repos(tmp_path: Path) -> None:
    data, store = tmp_path / "data", tmp_path / "store"
    data.mkdir()
    store.mkdir()
    state = load_plugin_state(data_dir=data, store_dir=store)
    assert state.plugin_repos == ()


def test_meta_sources_ship_with_no_active_source(tmp_path: Path) -> None:
    state = load_plugin_state(data_dir=tmp_path / "data", store_dir=tmp_path / "store")
    assert state.active_sources == ()
    assert state.active_meta_sources == ()
