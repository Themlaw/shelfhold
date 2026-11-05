"""The CLI picks its channel: the server's socket when it runs, the `flock` when it is stopped
(EF-USR-024, ENF-OPS-015)."""

import os
import re

import pytest

from shelfhold.config import Settings
from shelfhold.identity.application.service import LocalAuth
from shelfhold.infrastructure.instance import InstanceLock, holder_is_alive
from shelfhold.infrastructure.local_socket import Peer
from shelfhold.infrastructure.migrate import migrate
from tests.auth_env import auth_env, create_admin, db_rows, run_cli
from tests.local_socket_env import serving

pytestmark = pytest.mark.req("EF-USR-024", "ENF-OPS-015")
KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")


def _prepared(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    migrate(settings)
    assert create_admin(monkeypatch, settings) == 0


def test_cli_without_server_takes_flock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    _prepared(monkeypatch, settings)
    lock_path = settings.data_dir / ".lock"
    seen: list[bool] = []
    real = LocalAuth.api_keys

    def spy(self: LocalAuth) -> object:
        seen.append(holder_is_alive(lock_path))  # the lock is held while the command acts
        return real(self)

    monkeypatch.setattr(LocalAuth, "api_keys", spy)
    assert run_cli(monkeypatch, settings, "admin", "keys", "list") == 0
    assert seen == [True]
    assert not holder_is_alive(lock_path)  # and released when it is done


def test_offline_cli_audit_keeps_the_host_uid_in_the_target(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    _prepared(monkeypatch, settings)
    assert run_cli(monkeypatch, settings, "admin", "keys", "create") == 0
    [(target,)] = db_rows(settings, "SELECT target FROM audit WHERE action = 'api_key.created'")
    assert str(target).endswith(f" uid={os.getuid()}")


def test_cli_with_server_running_goes_through_the_socket(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        taken: list[str] = []
        monkeypatch.setattr(InstanceLock, "acquire", lambda self: taken.append("flock"))
        capsys.readouterr()
        assert run_cli(monkeypatch, env.settings, "admin", "keys", "create", "--name", "x") == 0
        out = capsys.readouterr().out
        assert KEY.search(out)
        assert taken == []  # the server owns the lock; the CLI never competes for it
        assert run_cli(monkeypatch, env.settings, "admin", "keys", "list") == 0
        assert "x" in capsys.readouterr().out
        rows = db_rows(env.settings, "SELECT actor_id, target FROM audit WHERE action LIKE 'api_key.%'")
        assert [r[0] for r in rows] == [f"local_cli:{os.getuid()}"]
        assert "uid=" not in str(rows)


def test_admin_keys_rotate_and_revoke_via_socket_from_the_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        assert run_cli(monkeypatch, env.settings, "admin", "keys", "create") == 0
        first = KEY.search(capsys.readouterr().out)
        assert first
        assert run_cli(monkeypatch, env.settings, "admin", "keys", "rotate", first.group(1)) == 0
        second = KEY.search(capsys.readouterr().out)
        assert second
        assert run_cli(monkeypatch, env.settings, "admin", "keys", "revoke", second.group(1)) == 0
        assert run_cli(monkeypatch, env.settings, "admin", "keys", "revoke", "deadbeef") == 1
        assert {a for (a,) in db_rows(env.settings, "SELECT actor_id FROM audit WHERE action LIKE 'api_key.%'")} == {
            f"local_cli:{os.getuid()}"
        }


def test_session_revocation_works_with_the_server_running(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        assert env.login().status_code == 200
        assert run_cli(monkeypatch, env.settings, "admin", "sessions", "revoke", "--all") == 0
        assert env.client.get("/api/v1/auth/session").status_code == 401


def test_refused_peer_is_reported_and_the_cli_does_not_fall_back_to_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    _prepared(monkeypatch, settings)
    outside = lambda _sock: Peer(pid=0, uid=os.geteuid(), gid=0)  # noqa: E731
    with serving(settings.data_dir / "shelfhold.sock", peer_reader=outside):
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "admin", "keys", "create") == 1
        assert "SHELFHOLD_CLI_ALLOW_HOST" in capsys.readouterr().err
    assert db_rows(settings, "SELECT count(*) FROM api_key") == [(0,)]


def test_stale_socket_with_stopped_server_falls_back_to_flock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    import socket

    _prepared(monkeypatch, settings)
    old = socket.socket(socket.AF_UNIX)
    old.bind(str(settings.data_dir / "shelfhold.sock"))
    old.close()  # a crashed server left its socket behind
    assert run_cli(monkeypatch, settings, "admin", "keys", "list") == 0


def test_revoking_a_session_still_works_when_the_socket_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    """PLAN-M1a L05: a stolen session is revoked without stopping the server, even while the
    server only answers `/healthz` (migration) and has no socket."""
    _prepared(monkeypatch, settings)
    with InstanceLock(settings.data_dir) as lock:
        assert lock.is_held()
        assert run_cli(monkeypatch, settings, "admin", "sessions", "revoke", "--all") == 0


def test_api_key_listing_never_prints_secrets_over_the_socket(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        assert run_cli(monkeypatch, env.settings, "admin", "keys", "create") == 0
        value = KEY.search(capsys.readouterr().out)
        assert value
        assert run_cli(monkeypatch, env.settings, "admin", "keys", "list", "--json") == 0
        listing = capsys.readouterr().out
        assert value.group(1) in listing
        assert value.group(0) not in listing
        assert "hash" not in listing
