"""The CLI side: one JSON-RPC request over the Unix socket, one answer."""

import contextlib
import json
import os
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from shelfhold.infrastructure.local_socket import addressing

MAX_REPLY_BYTES = 4 * 1024 * 1024


class ServerUnavailable(Exception):
    """Nothing listens on the socket (absent, or left behind by a dead server)."""


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


def call(
    path: Path, method: str, params: Mapping[str, object], *, timeout_s: float = 60.0
) -> object:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        try:
            _connect(sock, path)
        except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError) as error:
            raise ServerUnavailable(str(path)) from error
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
