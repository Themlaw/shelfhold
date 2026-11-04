"""ENF-LEG-001: fresh install ships zero plugin repos and no active source."""

from shelfhold.plugin_host.defaults import (
    default_active_sources,
    default_meta_sources_active,
    default_plugin_repos,
)


def test_fresh_install_has_zero_plugin_repos() -> None:
    assert default_plugin_repos() == ()


def test_meta_sources_ship_with_no_active_source() -> None:
    assert default_meta_sources_active() == ()
    assert default_active_sources() == ()
