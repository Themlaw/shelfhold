"""`shelfhold adopt --dry-run` shows what adoption would do and writes nothing (EF-INT-014)."""

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import export
from shelfhold.interfaces import cli
from shelfhold.interfaces.cli import adopt as adopt_cli
from tests.adopt_env import adoption
from tests.export_env import ExportEnv, export_env


def _records(env: ExportEnv) -> int:
    with env.engine.connect() as connection:
        count = connection.execute(sa.select(sa.func.count()).select_from(export.export_record))
        return int(count.scalar_one())


@pytest.mark.req("EF-INT-014", "EF-EXP-028")
def test_cli_adopt_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: dict[str, str]
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with export_env() as export_environment:
        a = adoption(export_environment)
        a.add_series("Dr Stone", ["Dr Stone - Ch.1.cbz", "Dr Stone - Ch.2 [Team].cbz"])
        before = a.state()
        session = adopt_cli.AdoptionSession(a.adopter, a.wired.client)
        monkeypatch.setattr(adopt_cli, "open_session", lambda resolved, destination: session)
        destination = export_environment.destination_id
        assert cli.main(["adopt", "--dry-run", "--destination", destination]) == 0
        out = capsys.readouterr().out
        assert (
            "Dr Stone" in out
            and "Dr Stone - Ch.2 [Team].cbz" in out
            and "Dr Stone - Ch.2.cbz" in out
        )
        assert a.state() == before and a.wired.fake.writes == []
        assert _records(export_environment) == 0
        # without --dry-run and without an explicit confirmation: refused, nothing is entered
        assert cli.main(["adopt", "--destination", destination]) == 2
        assert a.wired.fake.writes == [] and _records(export_environment) == 0
        assert cli.main(["adopt", "--confirm", "--destination", destination]) == 0
        assert _records(export_environment) == 2
        assert a.state() == before
