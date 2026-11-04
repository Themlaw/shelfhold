"""Command line wiring: exit codes, messages, `serve`."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from shelfhold import cli
from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, head_revision, migrate
from shelfhold.infrastructure.settings_store import SettingsStore, load_db_settings
from shelfhold.main import app_from_env


@pytest.fixture
def cli_env(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def test_migrate_command_reports_then_is_up_to_date(
    cli_env: dict[str, str], settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["migrate"]) == 0
    assert head_revision() in capsys.readouterr().out
    assert cli.main(["migrate"]) == 0
    assert "à jour" in capsys.readouterr().out
    assert current_revision(settings.database_path) == head_revision()


def test_migrate_command_exits_1_on_a_newer_schema(
    cli_env: dict[str, str], settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.data_dir.mkdir(parents=True)
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        db.execute("INSERT INTO alembic_version VALUES ('from_the_future')")
        db.commit()
    assert cli.main(["migrate"]) == 1
    assert "from_the_future" in capsys.readouterr().err


def test_migrate_command_exits_1_and_names_the_failed_revision(
    cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(_settings: Settings) -> None:
        raise cli.MigrationError("0002", "boom")

    monkeypatch.setattr(cli, "migrate", broken)
    assert cli.main(["migrate"]) == 1
    assert "0002" in capsys.readouterr().err


def test_invalid_configuration_exits_2(
    cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SHELFHOLD_LOG_LEVEL", "LOUD")
    assert cli.main(["config", "show"]) == 2
    assert "log_level" in capsys.readouterr().err


def test_serve_starts_uvicorn_with_the_resolved_host_and_port(
    cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELFHOLD_PORT", "9090")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    assert cli.main(["serve"]) == 0
    assert calls[0][0] == ("shelfhold.main:app_from_env",)
    assert calls[0][1]["port"] == 9090
    assert calls[0][1]["factory"] is True


def test_app_factory_reads_the_environment(cli_env: dict[str, str]) -> None:
    app = app_from_env()
    assert app.title == "Shelfhold"


def test_db_settings_are_empty_before_the_first_migration(settings: Settings) -> None:
    assert load_db_settings(settings.data_dir) == {}
    settings.data_dir.mkdir(parents=True)
    sqlite3.connect(settings.database_path).close()  # database without the table
    assert load_db_settings(settings.data_dir) == {}


def test_settings_store_updates_an_existing_key(settings: Settings) -> None:
    migrate(settings)
    store = SettingsStore(settings.database_path)
    store.set("log_level", "DEBUG")
    store.set("log_level", "ERROR")
    assert store.all() == {"log_level": "ERROR"}


def test_current_revision_is_none_for_an_unmigrated_database(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    sqlite3.connect(path).close()
    assert current_revision(path) is None
