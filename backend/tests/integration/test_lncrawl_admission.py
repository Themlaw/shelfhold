"""ENF-LEG-003: admission of the lncrawl adapter (D18)."""

from __future__ import annotations

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host.adapters.meta.lncrawl import LncrawlService
from shelfhold.plugin_host.settings import SourceSettings, parse_settings_schema


@pytest.fixture
def store(settings: Settings):
    migrate(settings)
    vault = SecretVault(boot_check(settings, announce=False))
    engine = create_db_engine(settings.database_path)
    yield engine, SourceSettings(engine, vault)
    engine.dispose()


@pytest.mark.req("ENF-LEG-003")
def test_lncrawl_rate_limit_is_one_request_per_second() -> None:
    from pathlib import Path

    service = LncrawlService()
    assert service.manifest.rate_limit.requests_per_second == 1.0
    assert service.manifest.rate_limit.concurrency == 1
    manifest_path = (
        Path(__file__).resolve().parents[3] / "sources" / "meta_lncrawl" / "manifest.json"
    )
    from shelfhold.sdk.source import SourceManifest

    on_disk = SourceManifest.model_validate_json(manifest_path.read_text())
    assert on_disk.rate_limit.requests_per_second == 1.0
    assert on_disk.rate_limit.concurrency == 1


@pytest.mark.req("ENF-LEG-003")
def test_lncrawl_disabled_on_fresh_instance(tmp_path) -> None:
    from shelfhold.config import load_settings
    from shelfhold.plugin_host.public import load_plugin_state

    env = {
        "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
    }
    resolved = load_settings(env)
    assert "lncrawl" not in resolved.values.active_sources
    state = load_plugin_state(resolved.values)
    assert "lncrawl" not in state.active_sources


@pytest.mark.req("ENF-LEG-003")
def test_lncrawl_settings_carry_an_admin_rights_warning(store) -> None:
    _engine, settings_store = store
    schema = parse_settings_schema(LncrawlService().manifest.settings_schema)
    view = settings_store.view("lncrawl", schema)
    assert view.warnings  # an admin-facing warning is present
