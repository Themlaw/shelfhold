"""EF-INT-027 (M3): `shelfhold follow export --format opml|csv` writes the followed series."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import acquisition
from tests.auth_env import run_cli
from tests.l51_env import live


@pytest.mark.req("EF-INT-027")
def test_follows_export_opml_csv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path,
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=("1", "2")) as api:
        with api.env.inst.engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.series_follow).values(
                    edition_id=api.series.edition_id,
                    work_id=api.series.work_id,
                )
            )
        capsys.readouterr()

        assert run_cli(monkeypatch, settings, "follow", "export", "--format", "opml") == 0
        opml = capsys.readouterr().out
        assert "<opml" in opml
        assert "Dr Stone" in opml

        assert run_cli(monkeypatch, settings, "follow", "export", "--format", "csv") == 0
        csv_out = capsys.readouterr().out
        assert "Dr Stone" in csv_out
