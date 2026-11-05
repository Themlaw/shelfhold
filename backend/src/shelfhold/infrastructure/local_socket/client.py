"""The CLI side: one JSON-RPC request over the Unix socket, one answer."""

import contextlib
import json
import os
import socket
import stat
import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from shelfhold.infrastructure.local_socket import addressing

MAX_REPLY_BYTES = 4 * 1024 * 1024


class ServerUnavailable(Exception):
    """Nothing listens on the socket (absent, or left behind by a dead server)."""


class UntrustedServer(Exception):
    """What listens on the socket is not owned by this user or root: nothing was sent to it.

    The socket lives in a directory the operator controls, and the request may carry a password
    or a new API key's name; so the server is checked before the first byte (EF-USR-024)."""


class RpcFailure(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _connect(sock: socket.socket, path: Path) -> None:
    if addressing.fits(path):
        sock.connect(str(path))
        return
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        sock.connect(addressing.via_directory(directory, path.name))
    finally:
        os.close(directory)


def _trusted(trusted_uids: frozenset[int] | None) -> frozenset[int]:
    return trusted_uids if trusted_uids is not None else frozenset({os.geteuid(), 0})


def _check_file(path: Path, trusted: frozenset[int]) -> None:
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise ServerUnavailable(str(path)) from error
    if not stat.S_ISSOCK(info.st_mode):
        raise UntrustedServer(
            f"{path} is not a socket (a link, a file or a directory was put there)"
        )
    if info.st_uid not in trusted:
        raise UntrustedServer(f"{path} belongs to uid {info.st_uid}, not to this user or root")


def _check_peer(sock: socket.socket, trusted: frozenset[int]) -> None:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    if uid not in trusted:
        raise UntrustedServer(f"the server answering on the socket runs as uid {uid}")


def call(
    path: Path,
    method: str,
    params: Mapping[str, object],
    *,
    timeout_s: float = 60.0,
    trusted_uids: frozenset[int] | None = None,
) -> object:
    trusted = _trusted(trusted_uids)
    _check_file(path, trusted)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        try:
            _connect(sock, path)
        except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError) as error:
            raise ServerUnavailable(str(path)) from error
        _check_peer(sock, trusted)  # the owner of the file is not always who accepts
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params)}
        sock.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        buffer = b""
        while not buffer.endswith(b"\n") and len(buffer) < MAX_REPLY_BYTES:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    try:
        reply = json.loads(buffer)
    except ValueError as error:
        raise ServerUnavailable("the server closed the connection without an answer") from error
    if "error" in reply:
        error_body = reply["error"]
        raise RpcFailure(error_body["code"], error_body["message"], error_body.get("data"))
    return reply["result"]
