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
