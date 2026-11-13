"""ENF-SEC-005: `shelfhold secrets rotate|status` end to end, and `serve` refusing to start."""

import json

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import metadata
from shelfhold.infrastructure.secrets import diagnostics, registry
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.interfaces import cli
from shelfhold.interfaces.cli import serve as serve_command


@pytest.fixture
def cli_env(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.mark.req("ENF-SEC-005")
def test_cli_rotate_reencrypts_and_asks_for_a_new_export(
    cli_env: dict[str, str], settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate(settings)
    keyring = diagnostics.boot_check(settings)
    table = metadata.tables["source_config"]
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            registry.sync_registry(connection, keyring)
            connection.execute(sa.insert(table).values(plugin_id="p"))
            SecretVault(keyring).seal_row(connection, "source_config", "p", b"s3cret")
        assert cli.main(["secrets", "rotate"]) == 0
        out = capsys.readouterr().out
        assert "1 secret rechiffré" in out
        assert "secrets export" in out  # the previous export no longer covers the new key
        after = diagnostics.store_for(settings).load()
        assert after.active.kid != keyring.active.kid
        with engine.connect() as connection:
            assert SecretVault(after).open_row(connection, "source_config", "p") == b"s3cret"
    finally:
        engine.dispose()
    assert "never_exported" in {w.code for w in diagnostics.collect_warnings(settings)}


@pytest.mark.req("ENF-SEC-005")
def test_cli_rotate_retire_refuses_the_active_key(
    cli_env: dict[str, str], settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate(settings)
    keyring = diagnostics.boot_check(settings)
    assert cli.main(["secrets", "rotate", "--retire", keyring.active.kid]) == 1
    assert "active" in capsys.readouterr().err
    assert cli.main(["secrets", "rotate", "--retire", "k-nope"]) == 1


@pytest.mark.req("ENF-SEC-005")
def test_cli_rotate_without_database_fails_cleanly(
    cli_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["secrets", "rotate"]) == 1
    assert "migrate" in capsys.readouterr().err


@pytest.mark.req("ENF-SEC-005")
def test_cli_status_lists_warnings_in_text(
    cli_env: dict[str, str], settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    diagnostics.boot_check(settings)
    assert cli.main(["secrets", "status"]) == 0
    out = capsys.readouterr().out
    assert "/data" in out
    json_code = cli.main(["secrets", "status", "--json"])
    assert json_code == 0
    assert {"key_in_data", "never_exported"} <= {
        w["code"] for w in json.loads(capsys.readouterr().out)["warnings"]
    }


@pytest.mark.req("ENF-SEC-005")
def test_serve_refuses_to_start_when_the_keyring_is_unreadable(
    cli_env: dict[str, str],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostics.boot_check(settings)
    started: list[bool] = []
    monkeypatch.setattr(serve_command.uvicorn, "run", lambda *a, **k: started.append(True))
    path = settings.data_dir / "keyring"
    path.write_bytes(b"corrupt")
    # a corrupt keyring is a different failure from an unreadable one: both stop the start
    assert cli.main(["serve"]) == 1
    assert started == []
    assert "Démarrage refusé" in capsys.readouterr().err
