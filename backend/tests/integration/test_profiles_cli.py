"""`shelfhold profile add|list|set|remove` (M3 L05, EF-USR-001, EF-USR-005, EF-USR-024).

Both channels: the host CLI with the server stopped (direct, under the lock) and through the
socket of a running server, where the principal is `local_cli` and the audit says so.
"""

import json
import os
from collections.abc import Iterator

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.local_socket import RpcFailure, call
from shelfhold.infrastructure.migrate import migrate
from tests.auth_env import ADMIN, PASSWORD, AuthEnv, auth_env, create_admin, db_rows, run_cli


def _password_file(settings: Settings, name: str = "profile.password") -> str:
    path = settings.data_dir.parent / name
    path.write_text(PASSWORD + "\n", encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _rows(settings: Settings) -> list[tuple[object, ...]]:
    return db_rows(
        settings, 'SELECT username, role, can_rate, max_age_rating FROM "user" ORDER BY id'
    )


@pytest.fixture
def stopped(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Settings:
    migrate(settings)
    assert create_admin(monkeypatch, settings) == 0
    return settings


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


@pytest.mark.req("EF-USR-001", "EF-USR-005")
def test_cli_profile_add_list_set_remove_with_the_server_stopped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stopped: Settings
) -> None:
    password = _password_file(stopped)
    add = ["profile", "add", "--password-file", password]
    assert run_cli(monkeypatch, stopped, *add, "mum", "--role", "member") == 0
    assert run_cli(monkeypatch, stopped, *add, "kid", "--role", "child", "--max-age", "10") == 0
    assert run_cli(monkeypatch, stopped, *add, "kid2", "--role", "child") == 1  # no ceiling
    assert (
        run_cli(
            monkeypatch, stopped, *add, "kid3", "--role", "child", "--max-age", "10", "--can-rate"
        )
        == 1
    )
    assert run_cli(monkeypatch, stopped, *add, "mum", "--role", "member") == 1  # taken
    with pytest.raises(SystemExit):  # argparse refuses an unknown role
        run_cli(monkeypatch, stopped, *add, "bad", "--role", "ghost")
    assert run_cli(monkeypatch, stopped, *add, "dad", "--role", "member", "--max-age", "5") == 1
    assert [r[0] for r in _rows(stopped)] == [ADMIN, "mum", "kid"]
    capsys.readouterr()
    assert run_cli(monkeypatch, stopped, "profile", "list", "--json") == 0
    listed = {p["username"]: p for p in json.loads(capsys.readouterr().out)}
    assert listed["kid"]["max_age_rating"] == 10 and listed["kid"]["role"] == "child"
    assert listed["mum"]["can_rate"] is False and "password" not in json.dumps(listed)
    assert run_cli(monkeypatch, stopped, "profile", "list") == 0
    assert "kid" in capsys.readouterr().out
    # set: the right to classify for an adult, the ceiling of a child, a source policy
    assert run_cli(monkeypatch, stopped, "profile", "set", "mum", "--can-rate") == 0
    assert run_cli(monkeypatch, stopped, "profile", "set", "kid", "--max-age", "6") == 0
    assert run_cli(monkeypatch, stopped, "profile", "set", "kid", "--max-age", "200") == 1
    assert run_cli(monkeypatch, stopped, "profile", "set", "nobody", "--max-age", "6") == 1
    assert (
        run_cli(
            monkeypatch, stopped, "profile", "set", "kid", "--source-policy", "gutenberg=forbidden"
        )
        == 0
    )
    assert (
        run_cli(
            monkeypatch,
            stopped,
            "profile",
            "set",
            "kid",
            "--source-policy",
            "gutenberg=add_directly",
        )
        == 1
    )
    with pytest.raises(SystemExit):  # not SOURCE=POLICY
        run_cli(monkeypatch, stopped, "profile", "set", "kid", "--source-policy", "nonsense")
    assert [r[1:] for r in _rows(stopped)] == [
        ("admin", 1, None),
        ("member", 1, None),
        ("child", 0, 6),
    ]
    # the host may name a second admin, never a third, and never leave the instance without one
    assert run_cli(monkeypatch, stopped, "profile", "set", "mum", "--role", "admin") == 0
    assert run_cli(monkeypatch, stopped, "profile", "set", "kid", "--role", "admin") == 1
    assert run_cli(monkeypatch, stopped, "profile", "remove", "mum") == 0
    assert run_cli(monkeypatch, stopped, "profile", "remove", "mum") == 1
    assert run_cli(monkeypatch, stopped, "profile", "remove", ADMIN) == 1
    assert run_cli(monkeypatch, stopped, "profile", "remove", "kid") == 0
    assert [r[0] for r in _rows(stopped)] == [ADMIN]


@pytest.mark.req("EF-USR-024", "EF-USR-005")
def test_cli_profile_through_the_socket_of_a_running_server_is_audited_as_local_cli(
    monkeypatch: pytest.MonkeyPatch, running: AuthEnv
) -> None:
    password = _password_file(running.settings)
    assert run_cli(monkeypatch, running.settings, "profile", "add", "--password-file", password,
                   "kid", "--role", "child", "--max-age", "9") == 0  # fmt: skip
    assert run_cli(monkeypatch, running.settings, "profile", "set", "kid", "--max-age", "7") == 0
    assert run_cli(monkeypatch, running.settings, "profile", "set", ADMIN, "--role", "member") == 1
    assert run_cli(monkeypatch, running.settings, "profile", "remove", "kid") == 0
    actors = db_rows(
        running.settings,
        "SELECT DISTINCT actor_id FROM audit WHERE action LIKE 'profile.%'",
    )
    assert actors == [(f"local_cli:{os.getuid()}",)]
    done = {
        a[0]
        for a in db_rows(running.settings, "SELECT action FROM audit WHERE action LIKE 'profile.%'")
    }
    assert {"profile.created", "profile.updated", "profile.removed"} <= done


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_the_socket_answers_a_refusal_with_its_code_and_never_a_password(
    running: AuthEnv,
) -> None:
    socket = running.settings.data_dir / "shelfhold.sock"
    with pytest.raises(RpcFailure) as refused:
        call(socket, "identity.profile_add",
             {"username": "kid", "password": PASSWORD, "role": "child"})  # fmt: skip
    assert (refused.value.data or {}).get("code") == "child_requires_max_age"
    assert PASSWORD not in str(refused.value)
    with pytest.raises(RpcFailure):
        call(
            socket,
            "identity.profile_add",
            {"username": "z", "password": PASSWORD, "role": "child", "max_age_rating": "ten"},
        )
    assert call(socket, "identity.profile_list", {})
    assert [r[0] for r in _rows(running.settings)] == [ADMIN]


@pytest.mark.req("EF-USR-005", "ENF-SEC-008")
def test_cli_never_prints_the_password_nor_what_was_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stopped: Settings
) -> None:
    for secret, role in (("tiny", "member"), (PASSWORD, "child")):
        path = stopped.data_dir.parent / "leak.password"
        path.write_text(secret + "\n", encoding="utf-8")
        path.chmod(0o600)
        capsys.readouterr()
        # too short, then a refusal of the rules (a child without ceiling)
        assert (
            run_cli(
                monkeypatch,
                stopped,
                "profile",
                "add",
                "--password-file",
                str(path),
                "leaky",
                "--role",
                role,
            )
            == 1
        )
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err
    unreadable = stopped.data_dir.parent / "missing.password"
    assert (
        run_cli(monkeypatch, stopped, "profile", "add", "--password-file", str(unreadable), "leaky")
        == 1
    )
    assert str(unreadable) not in capsys.readouterr().err
