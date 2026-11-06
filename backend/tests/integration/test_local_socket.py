"""The local CLI socket: creation, framing, limits and peer checks (EF-USR-024, ENF-OPS-015).

JSON-RPC, one line per message, over `/data/shelfhold.sock`. Never HTTP, never TCP.
"""

import json
import os
import shutil
import socket
import stat
import tempfile
import time
from pathlib import Path

import pytest
import structlog

from shelfhold.infrastructure.local_socket import Peer, RpcFailure, ServerUnavailable, call
from tests.local_socket_env import serving

pytestmark = pytest.mark.req("EF-USR-024", "ENF-OPS-015")


@pytest.fixture
def sock_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


def _raw(path: Path) -> socket.socket:
    raw = socket.socket(socket.AF_UNIX)
    raw.settimeout(5)
    raw.connect(str(path))
    return raw


def _reply(raw: socket.socket) -> dict[str, object]:
    buffer = b""
    while not buffer.endswith(b"\n"):
        chunk = raw.recv(4096)
        if not chunk:
            break
        buffer += chunk
    return json.loads(buffer) if buffer else {}


def test_socket_is_0600_whatever_the_umask(sock_dir: Path) -> None:
    previous = os.umask(0)  # the mode must not depend on the umask of the process
    try:
        with serving(sock_dir / "shelfhold.sock"):
            mode = stat.S_IMODE(os.lstat(sock_dir / "shelfhold.sock").st_mode)
            assert stat.S_ISSOCK(os.lstat(sock_dir / "shelfhold.sock").st_mode)
            assert mode == 0o600
            assert os.umask(0) == 0  # and the server did not play with the umask
    finally:
        os.umask(previous)


def test_socket_creation_leaves_no_staging_directory(sock_dir: Path) -> None:
    with serving(sock_dir / "shelfhold.sock"):
        assert [p.name for p in sock_dir.iterdir()] == ["shelfhold.sock"]
    assert list(sock_dir.iterdir()) == []  # the socket goes away with the server


def test_symlink_at_socket_path_is_replaced_never_followed(sock_dir: Path) -> None:
    victim = sock_dir / "victim"
    victim.write_text("precious")
    victim.chmod(0o640)
    (sock_dir / "shelfhold.sock").symlink_to(victim)
    with serving(sock_dir / "shelfhold.sock"):
        assert stat.S_ISSOCK(os.lstat(sock_dir / "shelfhold.sock").st_mode)
        assert victim.read_text() == "precious"
        assert stat.S_IMODE(victim.stat().st_mode) == 0o640
        assert call(sock_dir / "shelfhold.sock", "echo", {"a": 1}) == {"a": 1}


def test_stale_socket_from_a_dead_server_is_replaced(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    old = socket.socket(socket.AF_UNIX)
    old.bind(str(path))
    old.close()  # the file stays: a server that was killed
    with serving(path):
        assert call(path, "echo", {"b": 2}) == {"b": 2}


def test_directory_at_socket_path_is_refused_untouched(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    path.mkdir()
    (path / "keep").write_text("x")
    with pytest.raises(OSError), serving(path):
        pass
    assert (path / "keep").read_text() == "x"


def test_data_directory_path_longer_than_sun_path_works(tmp_path: Path) -> None:
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    path = deep / "shelfhold.sock"
    assert len(str(path)) > 108
    with serving(path):
        assert call(path, "echo", {"c": 3}) == {"c": 3}


def test_json_rpc_line_protocol(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    with serving(path):
        raw = _raw(path)
        raw.sendall(b'{"jsonrpc":"2.0","id":7,"method":"echo","params":{"x":[1,2]}}\n')
        assert _reply(raw) == {"jsonrpc": "2.0", "id": 7, "result": {"x": [1, 2]}}
        raw = _raw(path)
        raw.sendall(b"not json\n")
        assert _reply(raw)["error"]["code"] == -32700  # type: ignore[index]
        raw = _raw(path)
        raw.sendall(b"[1,2]\n")
        assert _reply(raw)["error"]["code"] == -32600  # type: ignore[index]
        raw = _raw(path)
        raw.sendall(b'{"jsonrpc":"2.0","id":1,"method":"nope"}\n')
        assert _reply(raw)["error"]["code"] == -32601  # type: ignore[index]
    with pytest.raises(ServerUnavailable):
        call(path, "echo", {})


def test_http_over_the_socket_is_not_served(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    with serving(path):
        raw = _raw(path)
        raw.sendall(b"GET /api/v1/about HTTP/1.1\r\nHost: x\r\n\r\n")
        reply = _reply(raw)
        assert reply["error"]["code"] == -32700  # type: ignore[index]
        assert b"HTTP/1.1" not in json.dumps(reply).encode()


def test_oversized_line_is_refused_and_connection_closed(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    with serving(path, max_line_bytes=1024):
        raw = _raw(path)
        raw.sendall(b'{"method":"echo","params":{"p":"' + b"a" * 5000 + b'"}}\n')
        assert _reply(raw)["error"]["code"] == -32004  # type: ignore[index]
        assert raw.recv(10) == b""  # closed
        assert call(path, "echo", {"ok": True}) == {"ok": True}  # the server is fine


def test_silent_client_is_dropped_after_the_read_timeout(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    with serving(path, read_timeout_s=0.3):
        raw = _raw(path)
        raw.sendall(b'{"method":"echo"')  # no newline, ever
        started = time.monotonic()
        assert _reply(raw)["error"]["code"] == -32003  # type: ignore[index]
        assert time.monotonic() - started < 3


def test_handler_timeout_is_enforced(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    with serving(path, handler_timeout_s=0.2):
        with pytest.raises(RpcFailure) as caught:
            call(path, "sleep", {"seconds": 5})
        assert caught.value.code == -32003


def test_number_of_connections_is_bounded(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    with serving(path, max_connections=2, read_timeout_s=30):
        held = [_raw(path), _raw(path)]
        time.sleep(0.2)  # both are being served, waiting for a line
        third = _raw(path)
        assert _reply(third)["error"]["code"] == -32002  # busy
        held[0].close()
        time.sleep(0.2)
        assert call(path, "echo", {"again": 1}) == {"again": 1}
        held[1].close()


def test_real_peer_credentials_are_the_callers(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    with serving(path):
        assert call(path, "whoami", {}) == {"uid": os.getuid(), "pid": os.getpid()}


def test_local_cli_refuses_peer_outside_container_pid_namespace(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    outside = lambda _sock: Peer(pid=0, uid=os.geteuid(), gid=0)  # noqa: E731
    with structlog.testing.capture_logs() as logs, serving(path, peer_reader=outside):
        with pytest.raises(RpcFailure) as caught:
            call(path, "whoami", {})
        assert caught.value.code == -32001
        assert caught.value.data == {"code": "peer_outside_pid_namespace"}
    refusals = [log for log in logs if log["event"] == "local_cli.peer_refused"]
    assert refusals
    assert refusals[0]["reason"] == "pid_namespace"
    assert refusals[0]["uid"] == os.geteuid()
    assert refusals[0]["log_level"] == "warning"


def test_host_peer_accepted_only_with_the_explicit_setting(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    outside = lambda _sock: Peer(pid=0, uid=os.geteuid(), gid=0)  # noqa: E731
    with serving(path, peer_reader=outside, allow_host=True):
        assert call(path, "whoami", {}) == {"uid": os.geteuid(), "pid": 0}


@pytest.mark.parametrize(
    ("uid", "accepted"),
    [(1000, True), (0, True), (1001, False), (65534, False)],
)
def test_only_the_server_uid_or_root_may_connect(sock_dir: Path, uid: int, accepted: bool) -> None:
    path = sock_dir / "shelfhold.sock"
    peer = lambda _sock: Peer(pid=4242, uid=uid, gid=0)  # noqa: E731
    with structlog.testing.capture_logs() as logs, serving(path, peer_reader=peer, server_uid=1000):
        if accepted:
            assert call(path, "whoami", {}) == {"uid": uid, "pid": 4242}
        else:
            with pytest.raises(RpcFailure) as caught:
                call(path, "whoami", {})
            assert caught.value.code == -32001
            assert any(log["event"] == "local_cli.peer_refused" for log in logs)


def test_the_socket_is_never_a_tcp_listener() -> None:
    from shelfhold.infrastructure.local_socket import server

    source = Path(server.__file__).read_text()
    assert "AF_INET" not in source
    assert "start_server(" not in source.replace("start_unix_server(", "")


# --- review of PR #49 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        b"[" * 30000 + b"]" * 30000 + b"\n",  # nested deeper than the parser's recursion limit
        b'{"method":"echo","params":{"n":' + b"9" * 5000 + b"}}\n",  # absurd integer
    ],
    ids=["deep", "huge-int"],
)
def test_hostile_json_is_a_parse_error_not_a_server_failure(sock_dir: Path, payload: bytes) -> None:
    path = sock_dir / "shelfhold.sock"
    with structlog.testing.capture_logs() as logs, serving(path, max_line_bytes=1 << 20):
        raw = _raw(path)
        raw.sendall(payload)
        assert _reply(raw)["error"]["code"] == -32700  # type: ignore[index]
        assert call(path, "echo", {"after": 1}) == {"after": 1}
    assert not [log for log in logs if log.get("exc_info")]  # no logged traceback


def test_a_connection_counts_until_it_is_closed_not_until_it_is_answered(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    with serving(path, max_connections=2, drain_deadline_s=0.6):
        trickling = []
        for _ in range(2):
            raw = _raw(path)
            raw.sendall(b'{"jsonrpc":"2.0","id":1,"method":"echo","params":{}}\n')
            assert _reply(raw)["result"] == {}  # answered ... and still connected
            trickling.append(raw)
        time.sleep(0.2)
        refused = _raw(path)
        assert _reply(refused)["error"]["code"] == -32002  # the places are still taken
        for raw in trickling:
            raw.sendall(b" ")  # keeps trickling
        time.sleep(0.8)  # the drain has a total deadline, whatever the client sends
        assert raw.recv(10) == b""  # hung up on
        assert call(path, "echo", {"later": 1}) == {"later": 1}


def test_drain_after_an_error_has_a_total_deadline(sock_dir: Path) -> None:
    path = sock_dir / "shelfhold.sock"
    with serving(path, drain_deadline_s=0.5, max_line_bytes=1024):
        raw = _raw(path)
        raw.sendall(b"x" * 4000)  # too long, no newline
        assert _reply(raw)["error"]["code"] == -32004  # type: ignore[index]
        started = time.monotonic()
        while time.monotonic() - started < 3:
            try:
                raw.sendall(b"y")  # one byte at a time, forever
            except OSError:
                break
            time.sleep(0.1)
        assert time.monotonic() - started < 2.5


def test_binding_does_not_depend_on_a_descriptor_number_when_the_path_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At start-up another thread can close and reuse the number of the staging directory's
    descriptor: `/proc/self/fd/N/s` then fails with ENOTDIR (seen in the redeploy E2E). A short
    path binds through the private directory's own name instead."""
    from shelfhold.infrastructure.local_socket import addressing
    from shelfhold.infrastructure.local_socket.server import bind_private

    def reused(_fd: int, _name: str) -> str:
        raise AssertionError("the descriptor path must not be used for a short path")

    monkeypatch.setattr(addressing, "via_directory", reused)
    short = Path(tempfile.mkdtemp(prefix="sh-"))
    listener, identity = bind_private(short / "s.sock")
    try:
        assert stat.S_ISSOCK(identity.st_mode) and stat.S_IMODE(identity.st_mode) == 0o600
    finally:
        listener.close()
        shutil.rmtree(short, ignore_errors=True)
