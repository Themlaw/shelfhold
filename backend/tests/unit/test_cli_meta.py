"""`shelfhold meta rebuild|check`: unit.json is regenerated from the database (ENF-OPS-020)."""

import json

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.interfaces import cli
from shelfhold.library.public import meta_path
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)


@pytest.fixture
def seeded(env: dict[str, str], settings: Settings, monkeypatch: pytest.MonkeyPatch) -> str:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    with engine.begin() as c:
        work = add_work(c)
        edition = add_edition(c, work)
        unit = add_unit(c, edition)
        add_variant(c, unit, add_binding(c, work, edition, add_source_item(c)))
    engine.dispose()
    return unit


@pytest.mark.req("ENF-OPS-020")
def test_meta_rebuild_writes_every_unit_json_and_check_reports_drift(
    seeded: str, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    path = meta_path(settings.data_dir, seeded)
    assert cli.main(["meta", "check"]) == 1  # the file is missing: drift
    assert seeded in capsys.readouterr().out
    assert cli.main(["meta", "rebuild"]) == 0 and path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["unit_id"] == seeded
    capsys.readouterr()
    assert cli.main(["meta", "check"]) == 0
    path.write_text("{}", encoding="utf-8")
    assert cli.main(["meta", "rebuild", "--unit", seeded]) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == "1.1"


@pytest.mark.req("ENF-OPS-020")
def test_meta_rebuild_refuses_an_unknown_unit(seeded: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["meta", "rebuild", "--unit", "01JC0000000000000000000000"]) == 1
    assert capsys.readouterr().err
