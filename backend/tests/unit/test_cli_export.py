"""`shelfhold export --dry-run` prints the plan and writes nothing (EF-INT-014)."""

import pytest

from shelfhold.interfaces import cli
from shelfhold.interfaces.cli import export as export_cli
from tests.export_env import export_env


@pytest.mark.req("EF-INT-014")
def test_cli_export_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: dict[str, str]
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        monkeypatch.setattr(export_cli, "open_pipeline", lambda resolved: env.pipeline)
        assert cli.main(["export", "--dry-run", "--destination", env.destination_id]) == 0
        out = capsys.readouterr().out
        assert "Dr Stone/Dr Stone - Ch.2.cbz" in out
        assert env.files() == []  # the plan is shown, nothing is written
        with env.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM fs_intent").scalar_one() == 0
        assert cli.main(["export", "--destination", env.destination_id]) == 2  # apply: not here
        assert env.files() == []
