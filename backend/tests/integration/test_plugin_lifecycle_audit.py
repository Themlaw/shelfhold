"""ENF-OBS-003 (M3 L34): installing, enabling, attesting and removing a plugin leave audit rows."""

from pathlib import Path

import pytest

from tests.plugin_guard_env import PHRASE, Cli, make_cli, plugin_wheel

ENABLE = {"SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS": "1"}


@pytest.mark.req("ENF-OBS-003")
def test_plugin_lifecycle_audited(tmp_path: Path) -> None:
    cli: Cli = make_cli(tmp_path)
    wheel = plugin_wheel(tmp_path, "demo-plug", "demo", tmp_path / "probe.jsonl", "demo_plug")
    assert cli.run("plugin", "install", str(wheel.path), "--sha256", wheel.sha256).returncode == 0
    done = cli.run("plugin", "enable", "demo-plug", typed=PHRASE + "\n", **ENABLE)
    assert done.returncode == 0, done.stderr
    assert cli.run("plugin", "attest", "demo", "content_trust", "publisher").returncode == 0
    # a refusal is audited as well
    assert cli.run("plugin", "enable", "demo-plug", typed="no\n", **ENABLE).returncode == 1
    assert cli.run("plugin", "disable", "demo-plug").returncode == 0
    assert cli.run("plugin", "remove", "demo-plug").returncode == 0
    rows = cli.audit_actions()
    actions = [a for a, _ in rows]
    wanted_actions = (
        "plugin.install", "plugin.enable", "plugin.enable_refused", "plugin.attested",
        "plugin.disable", "plugin.remove",
    )  # fmt: skip
    for wanted in wanted_actions:
        assert wanted in actions, (wanted, actions)
    # the target names the plugin and the wheel, never a secret or a path of the host
    install = next(t for a, t in rows if a == "plugin.install")
    assert "demo-plug" in install and wheel.sha256 in install
    assert str(tmp_path) not in install
