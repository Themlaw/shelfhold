"""M4 L26 (EF-INT-008, ENF-SEC-010): `shelfhold import tachibk`, the real command over a real
database. The host's command line has no account: the markers are the first admin's unless a
profile is named. The first command is a preview and writes nothing to the library; `--apply <id>`
writes it once. A file over a bound is refused whole, and no name of a source is ever printed."""

import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from tests import tachibk_env as env
from tests.auth_env import PASSWORD, create_admin, run_cli

pytestmark = pytest.mark.req("EF-INT-008", "ENF-SEC-010")


def _instance(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> sa.Engine:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    assert create_admin(monkeypatch, settings) == 0
    engine = create_db_engine(settings.database_path)
    local_auth(engine, settings).profile_add("mum", PASSWORD, "member")
    return engine


def _file(tmp_path: Path, data: bytes, name: str = "backup.tachibk") -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _rows(engine: sa.Engine, sql: str) -> list[tuple[object, ...]]:
    with engine.connect() as connection:
        return [tuple(r) for r in connection.execute(sa.text(sql))]


def test_cli_previews_then_applies_for_a_named_profile(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    engine = _instance(monkeypatch, settings)
    capsys.readouterr()
    path = _file(tmp_path, env.sample())
    assert run_cli(monkeypatch, settings, "import", "tachibk", path, "--profile", "mum") == 0
    out = capsys.readouterr().out
    assert "2 séries" in out and "5 chapitres" in out and "--apply" in out
    assert "SynthSource" not in out  # no name of a source is ever printed
    assert _rows(engine, "SELECT 1 FROM pending_source_item") == []  # a preview writes nothing
    batch = str(_rows(engine, "SELECT id FROM tachibk_import")[0][0])
    assert run_cli(monkeypatch, settings, "import", "tachibk", "--apply", batch) == 0
    assert "3 marqueurs" in capsys.readouterr().out
    mum = _rows(engine, "SELECT id FROM user WHERE username = 'mum'")[0][0]
    assert {r[0] for r in _rows(engine, "SELECT user_id FROM read_marker")} == {mum}
    assert len(_rows(engine, "SELECT 1 FROM pending_source_item")) == 2
    audited = {r[0] for r in _rows(engine, "SELECT action FROM audit")}
    assert "import.tachibk_applied" in audited
    # the same preview is not applied twice
    assert run_cli(monkeypatch, settings, "import", "tachibk", "--apply", batch) == 1
    assert "import.tachibk_applied" in capsys.readouterr().err


def test_cli_without_a_profile_is_the_first_admins(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    engine = _instance(monkeypatch, settings)
    assert run_cli(monkeypatch, settings, "import", "tachibk", _file(tmp_path, env.sample())) == 0
    batch = str(_rows(engine, "SELECT id FROM tachibk_import")[0][0])
    assert run_cli(monkeypatch, settings, "import", "tachibk", "--apply", batch) == 0
    owners = _rows(engine, "SELECT u.role FROM read_marker m JOIN user u ON u.id = m.user_id")
    assert {r[0] for r in owners} == {"admin"}


def test_cli_refuses_what_it_must_not_read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    engine = _instance(monkeypatch, settings)
    capsys.readouterr()
    good = _file(tmp_path, env.sample())
    run = lambda *a: run_cli(monkeypatch, settings, "import", "tachibk", *a)  # noqa: E731
    assert run(good, "--profile", "nobody") == 1  # an absent profile
    assert "import.tachibk_not_found" in capsys.readouterr().err
    assert run(str(tmp_path / "missing")) == 1  # a file that cannot be read
    assert "unreadable" in capsys.readouterr().err
    assert run(_file(tmp_path, env.bomb(70), "bomb.tachibk")) == 1  # a gzip bomb
    assert "decompressed_too_large" in capsys.readouterr().err
    future = env.tachibk(env.backup([], extra=env.ld(999, "later")))
    assert run(_file(tmp_path, future, "future.tachibk")) == 1  # a generation it does not know
    assert "import.tachibk_schema" in capsys.readouterr().err
    fifo = tmp_path / "pipe.tachibk"
    os.mkfifo(fifo)  # a pipe is no backup, and reading it must not hold the server
    assert run(str(fifo)) == 1 and "unreadable" in capsys.readouterr().err
    assert run("/dev/null") == 1 and "unreadable" in capsys.readouterr().err
    assert run() == 2 and run(good, "--apply", "x") == 2  # one of a file or --apply
    capsys.readouterr()
    assert _rows(engine, "SELECT 1 FROM tachibk_import") == []
    assert _rows(engine, "SELECT 1 FROM pending_source_item") == []
