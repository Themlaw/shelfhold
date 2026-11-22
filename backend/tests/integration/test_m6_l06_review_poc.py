"""M6 L06, relecture adverse: proofs of the confirmed defects (red at the head of the PR)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.plugin_host import install as guard
from tests.auth_env import CSRF_COOKIE, CSRF_HEADER, auth_env, db_rows
from tests.plugin_guard_env import PHRASE, build_wheel, make_cli, plugin_wheel

ENABLE = {"SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS": "1"}


@pytest.mark.req("EF-SRC-019", "ENF-OBS-003")
def test_refused_in_process_enable_changes_nothing(tmp_path: Path) -> None:
    """A refused `enable --in-process` (no variable, wrong phrase) is audited as a refusal and
    must leave the state as it was: the isolated grant and the recorded mode survive it."""
    cli = make_cli(tmp_path)
    wheel = plugin_wheel(tmp_path, "demo-plug", "demo", tmp_path / "probe.jsonl", "demo_plug")
    assert cli.run("plugin", "install", str(wheel.path), "--sha256", wheel.sha256).returncode == 0
    assert cli.run("plugin", "enable", "demo-plug").returncode == 0
    state = cli.data / "plugins" / "state.json"
    before = json.loads(state.read_text(encoding="utf-8"))
    assert "demo-plug" in before["enabled"]

    refused = cli.run("plugin", "enable", "demo-plug", "--in-process", typed=PHRASE + "\n")
    assert refused.returncode == 1  # no variable: refused...
    after = json.loads(state.read_text(encoding="utf-8"))
    assert after["installed"]["demo-plug"]["mode"] == "isolated"  # ...and nothing changed
    assert "demo-plug" in after["enabled"]

    wrong = cli.run("plugin", "enable", "demo-plug", "--in-process", typed="no\n", **ENABLE)
    assert wrong.returncode == 1
    after = json.loads(state.read_text(encoding="utf-8"))
    assert after["installed"]["demo-plug"]["mode"] == "isolated"
    assert "demo-plug" in after["enabled"]


@pytest.mark.req("ENF-OBS-003", "EF-USR-022")
def test_http_enable_and_disable_audited_with_plugin_and_right_action(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """Enabling third-party code over HTTP names the plugin in the audit (as the CLI does,
    `plugin.enable` with `name@sha256`); a disable is not recorded as an enable; and a refused
    enable (unknown plugin) leaves no row that says it was allowed."""
    wheel = build_wheel(
        tmp_path, "acme-plug", {"acme_plug/__init__.py": "plugin = None\n"},
        entry=("acme", "acme_plug:plugin"),
    )  # fmt: skip
    guard.install_wheel(guard.Layout.of(settings.data_dir), wheel.path, wheel.sha256)
    with auth_env(monkeypatch, settings) as api:
        assert api.login().status_code == 200
        path = "/api/v1/plugins/acme-plug/enabled"
        assert api.post(path).status_code == 200
        rows = db_rows(settings, "SELECT action, target FROM audit WHERE action LIKE 'plugin.%'")
        assert any("acme-plug" in str(target or "") for _action, target in rows), rows

        csrf = api.client.cookies.get(CSRF_COOKIE)
        done = api.client.delete(path, headers={CSRF_HEADER: csrf})
        assert done.status_code == 200, done.text
        rows = db_rows(settings, "SELECT action FROM audit WHERE action LIKE 'plugin.%'")
        assert [a for (a,) in rows].count("plugin.enable_isolated") <= 1, rows  # DELETE != enable

        before = len(db_rows(settings, "SELECT 1 FROM audit WHERE action = 'plugin.enable_isolated'"))
        assert api.post("/api/v1/plugins/nope/enabled").status_code == 404
        after = len(db_rows(settings, "SELECT 1 FROM audit WHERE action = 'plugin.enable_isolated'"))
        assert after == before  # nothing was enabled: no "allowed" enable row
