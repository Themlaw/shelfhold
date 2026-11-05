"""Review of PR #49: the client trusts nothing it did not verify, and the operator is warned."""

import contextlib
import json
import os
import socket
import threading
from pathlib import Path

import pytest
import structlog

from shelfhold.config import Settings
from shelfhold.infrastructure.instance import InstanceLock
from shelfhold.infrastructure.local_socket import ServerUnavailable, UntrustedServer, call
from shelfhold.infrastructure.volumes import ensure_volumes
from tests.auth_env import run_cli
from tests.local_socket_env import serving

pytestmark = pytest.mark.req("EF-USR-024")


def _listener(path: Path) -> tuple[socket.socket, list[bytes]]:
    """A server that only records what the client sent before it says anything."""
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen()

    def accept() -> None:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        connection.settimeout(0.5)
        with contextlib.suppress(OSError):
            received.append(connection.recv(4096))
        connection.close()

    threading.Thread(target=accept, daemon=True).start()
    return server, received


def test_client_refuses_a_socket_owned_by_someone_else_and_sends_nothing(tmp_path: Path) -> None:
    path = tmp_path / "shelfhold.sock"
    server, received = _listener(path)
    try:
        with pytest.raises(UntrustedServer):
            call(
                path, "identity.reset_password", {"password": "secret"}, trusted_uids=frozenset({1})
            )
    finally:
        server.close()
    assert received in ([], [b""])  # not a byte of the request


def test_client_refuses_a_file_or_a_link_in_place_of_the_socket(tmp_path: Path) -> None:
    path = tmp_path / "shelfhold.sock"
    path.write_text("x")
    with pytest.raises(UntrustedServer):
        call(path, "ping", {})
    path.unlink()
    real, _ = _listener(tmp_path / "real.sock")
    try:
        path.symlink_to(tmp_path / "real.sock")  # a link is not our socket either
        with pytest.raises(UntrustedServer):
            call(path, "ping", {})
    finally:
        real.close()


def test_client_checks_the_uid_of_the_accepting_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File owned by us, process behind it not: a server that took over the name."""
    path = tmp_path / "shelfhold.sock"
    server, received = _listener(path)
    from shelfhold.infrastructure.local_socket import client

    real = client._check_file
    monkeypatch.setattr(client, "_check_file", lambda p, t: real(p, frozenset({os.geteuid()})))
    try:
        with pytest.raises(UntrustedServer):
            call(path, "ping", {}, trusted_uids=frozenset({os.geteuid() + 1}))
    finally:
        server.close()
    assert received in ([], [b""])


def test_a_trusted_server_is_still_served(tmp_path: Path) -> None:
    path = tmp_path / "s.sock"
    with serving(path):
        assert call(path, "echo", {"a": 1}) == {"a": 1}
    with pytest.raises(ServerUnavailable):
        call(path, "echo", {})


def test_cli_reports_an_untrusted_socket_in_clear(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.data_dir.mkdir(parents=True)
    server, received = _listener(settings.data_dir / "shelfhold.sock")
    from shelfhold.infrastructure.local_socket import client

    monkeypatch.setattr(client, "_trusted", lambda _t: frozenset({os.geteuid() + 1}))
    try:
        assert run_cli(monkeypatch, settings, "admin", "keys", "create") == 1
    finally:
        server.close()
    assert "rien n'a été envoyé" in capsys.readouterr().err
    assert received in ([], [b""])


def test_writable_data_dir_is_warned_about_at_startup_and_in_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.data_dir.mkdir(parents=True)
    settings.data_dir.chmod(0o775)
    with structlog.testing.capture_logs() as logs:
        ensure_volumes(settings)
    assert any(log["event"] == "data_dir_writable_by_others" for log in logs)
    assert run_cli(monkeypatch, settings, "status", "--json") == 0
    codes = [w["code"] for w in json.loads(capsys.readouterr().out)["warnings"]]
    assert "data_dir_writable_by_others" in codes
    settings.data_dir.chmod(0o750)
    with structlog.testing.capture_logs() as logs:
        ensure_volumes(settings)
    assert not any(log["event"] == "data_dir_writable_by_others" for log in logs)


def test_cli_allow_host_is_a_startup_warning_and_a_status_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.data_dir.mkdir(parents=True)
    settings.data_dir.chmod(0o750)
    monkeypatch.setenv("SHELFHOLD_CLI_ALLOW_HOST", "true")
    assert run_cli(monkeypatch, settings, "status", "--json") == 0
    codes = [w["code"] for w in json.loads(capsys.readouterr().out)["warnings"]]
    assert "cli_allow_host" in codes
    monkeypatch.delenv("SHELFHOLD_CLI_ALLOW_HOST")
    assert run_cli(monkeypatch, settings, "status", "--json") == 0
    assert "cli_allow_host" not in capsys.readouterr().out


def test_server_logs_allow_host_at_startup(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], settings: Settings
) -> None:
    from tests.auth_env import auth_env

    allowed = settings.model_copy(update={"cli_allow_host": True})
    with auth_env(monkeypatch, allowed):
        pass
    captured = capfd.readouterr()
    assert "local_cli.allow_host_enabled" in captured.out + captured.err


# --- the lock-less exception is only for revocations ---------------------------------------


def _held(settings: Settings) -> InstanceLock:
    lock = InstanceLock(settings.data_dir)
    lock.acquire()  # an instance (or a migration) that has no socket
    return lock


def _prepared(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    from shelfhold.infrastructure.migrate import migrate
    from tests.auth_env import create_admin

    migrate(settings)
    assert create_admin(monkeypatch, settings) == 0


def test_lock_held_without_socket_refuses_everything_but_revocations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    import re

    from tests.auth_env import db_rows

    _prepared(monkeypatch, settings)
    assert run_cli(monkeypatch, settings, "admin", "keys", "create") == 0
    prefix = re.search(r"shk_([0-9a-f]{8})_", capsys.readouterr().out).group(1)  # type: ignore[union-attr]
    with _held(settings):
        assert run_cli(monkeypatch, settings, "admin", "keys", "create") == 1
        assert run_cli(monkeypatch, settings, "admin", "keys", "rotate", prefix) == 1
        assert run_cli(monkeypatch, settings, "admin", "keys", "list") == 1
        assert "tient le verrou" in capsys.readouterr().err
        assert db_rows(settings, "SELECT count(*) FROM api_key") == [(1,)]
        assert run_cli(monkeypatch, settings, "admin", "sessions", "revoke", "--all") == 0
        assert run_cli(monkeypatch, settings, "admin", "keys", "revoke", prefix) == 0
    assert db_rows(settings, "SELECT revoked_at IS NOT NULL FROM api_key") == [(1,)]


def test_revocation_refused_while_a_migration_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    import fcntl

    from shelfhold.infrastructure.migrate import LOCK_FILE_NAME

    _prepared(monkeypatch, settings)
    with _held(settings):
        descriptor = os.open(settings.data_dir / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            assert run_cli(monkeypatch, settings, "admin", "sessions", "revoke", "--all") == 1
            assert "migration" in capsys.readouterr().err
        finally:
            os.close(descriptor)


def test_symlink_on_the_lock_is_a_refusal_never_ignored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    _prepared(monkeypatch, settings)
    victim = settings.data_dir / "victim"
    victim.write_text("x")
    (settings.data_dir / ".lock").unlink(missing_ok=True)
    (settings.data_dir / ".lock").symlink_to(victim)
    for argv in (("sessions", "revoke", "--all"), ("keys", "list"), ("keys", "create")):
        assert run_cli(monkeypatch, settings, "admin", *argv) == 1
        assert "lien symbolique" in capsys.readouterr().err
    assert victim.read_text() == "x"


def test_schema_revision_is_read_after_the_lock_attempt(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from shelfhold.interfaces.cli import admin

    _prepared(monkeypatch, settings)
    order: list[str] = []
    acquire = InstanceLock.acquire
    auth = admin._auth
    monkeypatch.setattr(
        InstanceLock, "acquire", lambda self: (order.append("lock"), acquire(self))[1]
    )
    monkeypatch.setattr(admin, "_auth", lambda s: (order.append("revision"), auth(s))[1])
    assert run_cli(monkeypatch, settings, "admin", "keys", "list") == 0
    assert order == ["lock", "revision"]
