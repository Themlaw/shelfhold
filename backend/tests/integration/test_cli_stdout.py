"""stdout is the output of a command: a JSON document with `--json`, a list. No line of a log is
mixed into it, whether or not the command configures its own logs (a Huey worker, a service of the
command, a library: whoever logs while the command prints)."""

from __future__ import annotations

import json

import pytest
import structlog

from shelfhold.config import Settings
from tests.auth_env import run_cli

pytestmark = pytest.mark.req("ENF-OPS-003")


def test_the_logs_of_a_command_that_configures_none_go_to_stderr_never_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from shelfhold.interfaces.cli import status

    def run(*_args: object) -> int:
        log = structlog.get_logger()
        log.info("noise.info")
        log.warning("noise.warning")
        print(json.dumps({"ok": True}))
        return 0

    monkeypatch.setattr(status, "run", run)
    assert run_cli(monkeypatch, settings, "status", "--json") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True}, captured.out  # nothing else on stdout
    assert "noise.warning" in captured.err  # a warning still tells
    assert "noise.info" not in captured.err  # `status` configures nothing: only WARNING and up
