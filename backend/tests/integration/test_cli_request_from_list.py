"""M4 L31 (EF-USR-020, ENF-SEC-010): `shelfhold request --from-list`, the real command over a real
database. The host's command line has no account: its own list is the first admin's, and a list for
a child is the reserved action `reading_shelf.attach` (the host is allowed it). Nothing is searched
in a source, a list over a bound is refused whole, and no profile but a child is made a list."""

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.migrate import migrate
from tests.auth_env import PASSWORD, create_admin, run_cli
from tests.integration.test_schema_catalog import add_edition, add_work

pytestmark = pytest.mark.req("EF-USR-020")
ISBN = "978-0-306-40615-7"  # the well-known sample number, no real book


def _instance(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> sa.Engine:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    assert create_admin(monkeypatch, settings) == 0
    engine = create_db_engine(settings.database_path)
    auth = local_auth(engine, settings)
    auth.profile_add("kid", PASSWORD, "child", max_age_rating=6)
    auth.profile_add("mum", PASSWORD, "member")
    with write_transaction(engine) as connection:
        add_edition(connection, add_work(connection, title="Tale of Orchard", sort_title="tale"))
    return engine


def _file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "summer.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _rows(engine: sa.Engine, sql: str) -> list[tuple[object, ...]]:
    with engine.connect() as connection:
        return [tuple(r) for r in connection.execute(sa.text(sql))]


def test_cli_makes_the_admins_list_from_a_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    engine = _instance(monkeypatch, settings)
    text = f"Tale of Orchard\nA Missing Tale (annotated edition)\nAnother ; {ISBN}\n"
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "request", "--from-list", _file(tmp_path, text)) == 0
    out = capsys.readouterr().out
    assert "lecture)\u202f: 1" in out
    assert "annotée précise\u202f: 1" in out  # a warning, not a refusal
    assert "demandés\u202f: 2" in out and "/a-approuver/liste/" in out
    assert _rows(engine, "SELECT name, outcome FROM request_list_import, request_list_item") == [
        ("summer", "in_library"), ("summer", "requested"), ("summer", "requested"),
    ]  # fmt: skip
    owner = _rows(engine, "SELECT u.role FROM request r JOIN user u ON u.id = r.requester_user_id")
    assert owner == [("admin",), ("admin",)]  # the first admin's own: the host has no account
    assert _rows(engine, "SELECT 1 FROM collection WHERE kind = 'reading_list'") == [(1,)]


def test_cli_makes_a_child_her_shelf_and_requests(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    engine = _instance(monkeypatch, settings)
    capsys.readouterr()
    args = ("request", "--from-list", _file(tmp_path, "Missing One\nMissing Two\n"))
    assert run_cli(monkeypatch, settings, *args, "--for-profile", "kid", "--name", "Kid list") == 0
    kid = _rows(engine, "SELECT id FROM user WHERE username = 'kid'")[0][0]
    assert _rows(engine, "SELECT owner_id, name FROM collection") == [(kid, "Kid list")]
    assert {r[0] for r in _rows(engine, "SELECT requester_user_id FROM request")} == {kid}
    audited = {r[0] for r in _rows(engine, "SELECT action FROM audit")}
    assert "reading_shelf.attached" in audited
    # the same list again: the same two requests, never searched, never duplicated
    assert run_cli(monkeypatch, settings, *args, "--for-profile", "kid") == 0
    assert len(_rows(engine, "SELECT 1 FROM request")) == 2


def test_cli_refuses_what_it_must_not_make(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    engine = _instance(monkeypatch, settings)
    good = _file(tmp_path, "One title\n")
    capsys.readouterr()
    # an adult is nobody's to make a list for, an absent profile does not exist
    assert (
        run_cli(monkeypatch, settings, "request", "--from-list", good, "--for-profile", "mum") == 1
    )
    assert "request.not_allowed" in capsys.readouterr().err
    assert run_cli(monkeypatch, settings, "request", "--from-list", good, "--for-profile", "x") == 1
    assert "request.not_found" in capsys.readouterr().err
    # a file that is missing, over 64 KiB, not text, or with too many lines: refused whole
    assert run_cli(monkeypatch, settings, "request", "--from-list", str(tmp_path / "no")) == 2
    assert "unreadable" in capsys.readouterr().err
    assert (
        run_cli(monkeypatch, settings, "request", "--from-list", _file(tmp_path, "x" * 70000)) == 2
    )
    assert "too_large" in capsys.readouterr().err
    (tmp_path / "bin.txt").write_bytes(b"\xff\xfe\x00")
    assert run_cli(monkeypatch, settings, "request", "--from-list", str(tmp_path / "bin.txt")) == 2
    assert "not_text" in capsys.readouterr().err
    many = _file(tmp_path, "\n".join(f"T{i}" for i in range(101)))
    assert run_cli(monkeypatch, settings, "request", "--from-list", many) == 1
    assert "request.invalid_list" in capsys.readouterr().err
    assert _rows(engine, "SELECT 1 FROM request") == []
    assert _rows(engine, "SELECT 1 FROM request_list_import") == []


def test_cli_reads_the_standard_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    engine = _instance(monkeypatch, settings)
    capsys.readouterr()
    code = run_cli(monkeypatch, settings, "request", "--from-list", "-", stdin="Piped Title\n")
    assert code == 0
    assert _rows(engine, "SELECT name FROM request_list_import") == [("Reading list",)]
