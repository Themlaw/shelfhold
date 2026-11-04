"""ENF-OPS-004 (precedence, *_FILE, read-only fields) and ENF-OPS-012 (volumes)."""

import json
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import ConfigError, Settings, load_settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.settings_store import SettingsStore
from shelfhold.interfaces.cli import main as cli_main
from shelfhold.main import create_app


@pytest.mark.req("ENF-OPS-004")
@pytest.mark.parametrize(
    ("in_env", "in_toml", "in_db", "expected", "source"),
    [
        ("DEBUG", "ERROR", "WARNING", "DEBUG", "env"),
        (None, "ERROR", "WARNING", "ERROR", "toml"),
        (None, None, "WARNING", "WARNING", "db"),
        (None, None, None, "INFO", "default"),
    ],
)
def test_config_precedence_env_over_toml_over_db(
    env: dict[str, str],
    in_env: str | None,
    in_toml: str | None,
    in_db: str | None,
    expected: str,
    source: str,
) -> None:
    if in_env:
        env["SHELFHOLD_LOG_LEVEL"] = in_env
    if in_toml:
        data_dir = Path(env["SHELFHOLD_DATA_DIR"])
        data_dir.mkdir(parents=True)
        (data_dir / "config.toml").write_text(
            f'schema_version = "1.0"\nlog_level = "{in_toml}"\n', encoding="utf-8"
        )
    db_values = {"log_level": in_db} if in_db else {}
    resolved = load_settings(env, db_loader=lambda _data_dir: db_values)
    assert resolved.values.log_level == expected
    assert resolved.fields["log_level"].source == source


@pytest.mark.req("ENF-OPS-004")
def test_file_suffix_secret_is_read(env: dict[str, str], tmp_path: Path) -> None:
    secret_file = tmp_path / "token_file"
    secret_file.write_text("s3cret-value\n", encoding="utf-8")
    env["SHELFHOLD_API_TOKEN_FILE"] = str(secret_file)
    resolved = load_settings(env)
    assert resolved.values.api_token is not None
    assert resolved.values.api_token.get_secret_value() == "s3cret-value"
    assert resolved.fields["api_token"].readonly
    assert "s3cret-value" not in repr(resolved.values)


@pytest.mark.req("ENF-OPS-004")
def test_file_suffix_conflicts_and_missing_files_are_refused(
    env: dict[str, str], tmp_path: Path
) -> None:
    secret_file = tmp_path / "token_file"
    secret_file.write_text("x", encoding="utf-8")
    both = {
        **env,
        "SHELFHOLD_API_TOKEN": "y",
        "SHELFHOLD_API_TOKEN_FILE": str(secret_file),
    }
    with pytest.raises(ConfigError, match="API_TOKEN"):
        load_settings(both)
    missing = {**env, "SHELFHOLD_API_TOKEN_FILE": str(tmp_path / "absent")}
    with pytest.raises(ConfigError, match="API_TOKEN_FILE"):
        load_settings(missing)


@pytest.mark.req("ENF-OPS-004")
def test_invalid_value_is_a_config_error(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="log_level"):
        load_settings({**env, "SHELFHOLD_LOG_LEVEL": "LOUD"})


@pytest.mark.req("ENF-OPS-004")
def test_env_defined_field_reported_readonly(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env["SHELFHOLD_LOG_LEVEL"] = "DEBUG"
    resolved = load_settings(env, db_loader=lambda _data_dir: {"public_url": "https://x.example"})
    assert resolved.fields["log_level"].readonly is True
    assert resolved.fields["public_url"].readonly is False
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli_main(["config", "show", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    by_name = {field["name"]: field for field in shown["fields"]}
    assert by_name["log_level"]["readonly"] is True
    assert by_name["log_level"]["source"] == "env"
    assert by_name["public_url"]["readonly"] is False


@pytest.mark.req("ENF-OPS-004")
def test_settings_store_roundtrip_feeds_the_db_layer(env: dict[str, str]) -> None:
    settings = load_settings(env).values
    migrate(settings)
    store = SettingsStore(settings.database_path)
    store.set("log_level", "WARNING")
    resolved = load_settings(env, db_loader=lambda _data_dir: store.all())
    assert resolved.values.log_level == "WARNING"
    assert resolved.fields["log_level"].source == "db"


SECRET_FIELDS = [
    name for name, field in Settings.model_fields.items() if "SecretStr" in str(field.annotation)
]


@pytest.mark.req("ENF-OPS-004", "ENF-SEC-005")
def test_there_is_at_least_one_secret_field() -> None:
    assert SECRET_FIELDS


@pytest.mark.req("ENF-OPS-004", "ENF-SEC-005")
@pytest.mark.parametrize("field", SECRET_FIELDS)
@pytest.mark.parametrize("as_json", [False, True])
def test_every_secret_value_is_masked_by_config_show(
    field: str,
    as_json: bool,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env[f"SHELFHOLD_{field.upper()}"] = "top-secret"
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli_main(["config", "show", *(["--json"] if as_json else [])]) == 0
    assert "top-secret" not in capsys.readouterr().out


@pytest.mark.req("ENF-OPS-004")
def test_secret_file_that_is_not_utf8_is_a_config_error(
    env: dict[str, str], tmp_path: Path
) -> None:
    secret_file = tmp_path / "binary"
    secret_file.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigError, match="API_TOKEN_FILE"):
        load_settings({**env, "SHELFHOLD_API_TOKEN_FILE": str(secret_file)})


@pytest.mark.req("ENF-OPS-004")
@pytest.mark.parametrize(("mode", "warned"), [(0o644, True), (0o640, False), (0o600, False)])
def test_world_readable_secret_file_is_reported(
    env: dict[str, str], tmp_path: Path, mode: int, warned: bool
) -> None:
    secret_file = tmp_path / "token"
    secret_file.write_text("value", encoding="utf-8")
    secret_file.chmod(mode)
    resolved = load_settings({**env, "SHELFHOLD_API_TOKEN_FILE": str(secret_file)})
    assert (str(secret_file) in resolved.world_readable_secret_files) is warned


@pytest.mark.req("ENF-OPS-004")
def test_cli_warns_in_french_about_a_world_readable_secret_file(
    env: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_file = tmp_path / "token"
    secret_file.write_text("value", encoding="utf-8")
    secret_file.chmod(0o644)
    env["SHELFHOLD_API_TOKEN_FILE"] = str(secret_file)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli_main(["config", "show"]) == 0
    assert "lisible par tous" in capsys.readouterr().err


@pytest.mark.req("ENF-OPS-012")
def test_data_and_store_volumes_distinct_and_configurable(env: dict[str, str]) -> None:
    defaults = load_settings({}).values
    assert defaults.data_dir == Path("/data")
    assert defaults.store_dir == Path("/store")

    settings = load_settings(env).values
    assert settings.data_dir == Path(env["SHELFHOLD_DATA_DIR"])
    assert settings.store_dir == Path(env["SHELFHOLD_STORE_DIR"])
    assert settings.data_dir != settings.store_dir

    same = {**env, "SHELFHOLD_STORE_DIR": env["SHELFHOLD_DATA_DIR"]}
    with pytest.raises(ConfigError, match="distinct"):
        load_settings(same)
    nested = {**env, "SHELFHOLD_STORE_DIR": env["SHELFHOLD_DATA_DIR"] + "/store"}
    with pytest.raises(ConfigError, match="distinct"):
        load_settings(nested)

    with TestClient(create_app(settings)):
        pass
    assert settings.data_dir.is_dir()
    assert settings.store_dir.is_dir()
    config = tomllib.loads((settings.data_dir / "config.toml").read_text(encoding="utf-8"))
    assert config["schema_version"] == "1.0"
    assert not (settings.store_dir / "config.toml").exists()
