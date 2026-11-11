"""M2 L07: the `plugin` CLI in process (EF-SRC-013, EF-EXP-065) — attest and withdraw, show and
change settings, set a secret from a file, set and clear the cookie jar. No value is printed."""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.plugin_host.attestation import InstalledVersion
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.sdk.models import API_VERSION
from shelfhold.sdk.testing.fakes import FakeRangedConnectedService
from tests.auth_env import run_cli
from tests.redaction_support import forget_secret_values

pytestmark = pytest.mark.req("EF-SRC-013", "EF-EXP-065")

KEY = "acme"
DIGEST = "sha256:" + "e" * 64
SCHEMA = {
    "type": "object",
    "properties": {
        "lang": {"type": "string", "enum": ["fr", "en"], "default": "fr"},
        "api_key": {"type": "string", "writeOnly": True},
    },
}


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Settings]:
    forget_secret_values()
    migrate(settings)
    boot_check(settings, announce=False)
    raw = FakeRangedConnectedService().manifest.model_dump(mode="json")
    raw = {**raw, "key": KEY, "api": API_VERSION, "settings_schema": SCHEMA}

    def start(*_a: object, **_k: object) -> tuple[PluginRegistry, list[Any]]:
        registry = PluginRegistry()
        candidate = PluginCandidate(
            KEY, PluginOrigin.FIRST_PARTY, lambda: SimpleNamespace(manifest=raw)
        )
        return registry, registry.load_all([candidate])

    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.start_plugins", start)
    monkeypatch.setattr(
        "shelfhold.interfaces.cli.plugin.installed_versions",
        lambda _settings: {KEY: InstalledVersion(KEY, "acme-plug", "2.0", DIGEST)},
    )
    yield settings
    forget_secret_values()


def _audit(settings: Settings) -> list[tuple[str, str]]:
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            rows = connection.execute(sa.text("SELECT action, target FROM audit ORDER BY rowid"))
            return [(r.action, r.target) for r in rows]
    finally:
        engine.dispose()


def test_cli_attest_and_withdraw(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cli: Settings
) -> None:
    assert run_cli(monkeypatch, cli, "plugin", "attest", KEY, "egress", "externe") == 1
    assert run_cli(monkeypatch, cli, "plugin", "attest", "suwayomi", "egress", "foyer") == 1
    assert run_cli(monkeypatch, cli, "plugin", "attest", KEY, "egress", "foyer") == 0
    assert "2.0" in capsys.readouterr().out
    assert run_cli(monkeypatch, cli, "plugin", "withdraw", KEY, "egress") == 0
    assert run_cli(monkeypatch, cli, "plugin", "withdraw", KEY, "egress") == 0
    assert [a for a in _audit(cli) if a[0].startswith("plugin.")] == [
        ("plugin.attested", f"{KEY}@2.0:egress=foyer"),
        ("plugin.attestation_withdrawn", f"{KEY}@2.0:egress=foyer"),
    ]


def test_cli_settings_secret_and_cookies_never_print_a_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli: Settings,
    tmp_path: Path,
) -> None:
    value, cookie = secrets.token_hex(16), secrets.token_hex(16)
    secret_file = tmp_path / "value"
    secret_file.write_text(value + "\n", encoding="utf-8")
    jar = tmp_path / "jar.json"
    jar.write_text(json.dumps({"sid": cookie}), encoding="utf-8")
    assert run_cli(monkeypatch, cli, "plugin", "settings", KEY) == 0
    shown = capsys.readouterr().out
    assert '"fr"' in shown
    assert run_cli(monkeypatch, cli, "plugin", "set", KEY, 'lang="en"') == 0
    assert run_cli(monkeypatch, cli, "plugin", "set", KEY, "lang=de") == 1
    assert run_cli(monkeypatch, cli, "plugin", "set", KEY, "lang") == 1
    assert run_cli(monkeypatch, cli, "plugin", "set-secret", KEY, "api_key",
                   "--secret-file", str(secret_file)) == 0  # fmt: skip
    assert run_cli(monkeypatch, cli, "plugin", "set-secret", KEY, "api_key",
                   "--secret-file", str(tmp_path / "missing")) == 1  # fmt: skip
    assert run_cli(monkeypatch, cli, "plugin", "set-cookies", KEY,
                   "--cookies-file", str(jar)) == 0  # fmt: skip
    assert run_cli(monkeypatch, cli, "plugin", "set-cookies", KEY, "--cookies-file", "-",
                   stdin="[1]") == 1  # fmt: skip
    assert run_cli(monkeypatch, cli, "plugin", "set-cookies", KEY, "--cookies-file", "-",
                   stdin="{}") == 0  # fmt: skip
    assert run_cli(monkeypatch, cli, "plugin", "settings", "nobody") == 1
    out = capsys.readouterr()
    printed = out.out + out.err
    assert '"en"' in printed
    assert value not in printed and cookie not in printed
    assert [a for a, _t in _audit(cli) if a.startswith("source.")] == [
        "source.settings_changed", "source.secret_set", "source.cookies_set",
        "source.cookies_cleared",
    ]  # fmt: skip


def test_cli_without_a_database_or_keyring(monkeypatch: pytest.MonkeyPatch, cli: Settings) -> None:
    keyring = [p for p in cli.data_dir.rglob("*") if p.is_file() and "key" in p.name]
    for path in keyring:
        path.unlink()
    assert run_cli(monkeypatch, cli, "plugin", "settings", KEY) == 1
    cli.database_path.unlink()
    assert run_cli(monkeypatch, cli, "plugin", "attest", KEY, "egress", "foyer") == 1
    assert run_cli(monkeypatch, cli, "plugin", "withdraw", KEY, "egress") == 1
