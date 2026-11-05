"""`asyncio` server on `/data/shelfhold.sock` (EF-USR-024, ENF-OPS-015, ENF-SEC-010).

Creation. The socket is bound inside a private directory (0700, created by this process), made
0600 there, then moved onto its final name with one `rename`: no instant where it exists with a
wider mode, no dependence on the umask, and whatever sits at the final name (a stale socket, a
symbolic link, a file) is replaced, never followed. A directory at that name is refused. The
caller holds the instance `flock`, so any previous socket is a dead server's.

Peers. `SO_PEERCRED` gives the pid, uid and gid of the connecting process. The uid must be the
server's or root. A pid of 0 means the peer lives outside this container's PID namespace (the
host, another container that mounts `/data`): refused unless `SHELFHOLD_CLI_ALLOW_HOST` is set
(GAP-13-07). Every refusal is logged.

Framing. One JSON-RPC 2.0 request per line, one response line, then the connection closes. The
line, the wait for it, the handler and the number of connections are all bounded.
"""

import asyncio
import contextlib
import json
import os
import secrets
import socket
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NamedTuple

import structlog

from shelfhold.infrastructure.local_socket import addressing

SOCKET_NAME = "shelfhold.sock"
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
APPLICATION_ERROR = -32000
PEER_REFUSED = -32001
BUSY = -32002
TIMEOUT = -32003
LINE_TOO_LONG = -32004
_DRAIN_BYTES = 256 * 1024  # what a refused client may still send before we hang up


class Peer(NamedTuple):
    pid: int
    uid: int
    gid: int


PeerReader = Callable[[Any], Peer]
Handler = Callable[[Peer, str, dict[str, Any]], Awaitable[object]]


class RpcError(Exception):
    """An error the handler wants the client to see: a code, a message, optional data."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def read_peer(sock: Any) -> Peer:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return Peer(pid=pid, uid=uid, gid=gid)


def _response(identifier: object, **body: object) -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "id": identifier, **body}, separators=(",", ":")).encode()
        + b"\n"
    )


def _failure(identifier: object, code: int, message: str, data: object = None) -> bytes:
    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return _response(identifier, error=error)


def bind_private(path: Path, *, backlog: int = 16) -> tuple[socket.socket, os.stat_result]:
    """Create the listening socket at `path` atomically; see the module docstring."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    staging = f".shelfhold-sock-{secrets.token_hex(8)}"
    listener: socket.socket | None = None
    staged = False
    try:
        os.mkdir(staging, 0o700, dir_fd=directory)  # the umask can only narrow it
        staged = True
        inner = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=directory)
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(addressing.via_directory(inner, "s"))
            os.chmod(addressing.via_directory(inner, "s"), 0o600)
            listener.listen(backlog)
            listener.setblocking(False)
            # rename(2) replaces a symbolic link itself and fails on a directory (EISDIR)
            os.rename("s", path.name, src_dir_fd=inner, dst_dir_fd=directory)
            identity = os.lstat(path.name, dir_fd=directory)
        finally:
            os.close(inner)
        return listener, identity
    except BaseException:
        if listener is not None:
            listener.close()
        raise
    finally:
        if staged:
            with contextlib.suppress(OSError):
                os.unlink(f"{staging}/s", dir_fd=directory)
            with contextlib.suppress(OSError):
                os.rmdir(staging, dir_fd=directory)
        os.close(directory)


class LocalSocketServer:
    def __init__(
        self,
        path: Path,
        handler: Handler,
        *,
        allow_host: bool = False,
        server_uid: int | None = None,
        peer_reader: PeerReader = read_peer,
        max_line_bytes: int = 64 * 1024,
        read_timeout_s: float = 10.0,
        handler_timeout_s: float = 60.0,
        max_connections: int = 8,
        drain_deadline_s: float = 1.0,
    ) -> None:
        self.path = path
        self._handler = handler
        self._allow_host = allow_host
        self._server_uid = os.geteuid() if server_uid is None else server_uid
        self._peer_reader = peer_reader
        self._max_line = max_line_bytes
        self._read_timeout = read_timeout_s
        self._handler_timeout = handler_timeout_s
        self._max_connections = max_connections
        self._drain_deadline = drain_deadline_s
        self._active = 0
        self._server: asyncio.Server | None = None
        self._identity: os.stat_result | None = None

    async def start(self) -> None:
        listener, self._identity = bind_private(self.path, backlog=self._max_connections * 2)
        try:
            self._server = await asyncio.start_unix_server(
                self._serve, sock=listener, limit=self._max_line
            )
        except BaseException:
            listener.close()
            self._remove_socket()
            raise

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server.close_clients()
            await self._server.wait_closed()
            self._server = None
        self._remove_socket()

    def _remove_socket(self) -> None:
        """Remove our socket, and only ours: a successor may already have replaced it."""
        if self._identity is None:
            return
        with contextlib.suppress(OSError):
            current = os.lstat(self.path)
            if (current.st_dev, current.st_ino) == (self._identity.st_dev, self._identity.st_ino):
                os.unlink(self.path)
        self._identity = None

    # --- one connection ----------------------------------------------------------------------

    def _refusal(self, peer: Peer) -> bytes | None:
        reason = None
        if peer.pid == 0 and not self._allow_host:
            reason = "pid_namespace"
            reply = _failure(
                None,
                PEER_REFUSED,
                "the peer is outside this container's PID namespace; set "
                "SHELFHOLD_CLI_ALLOW_HOST=1 to accept the host (that uid is then an admin)",
                {"code": "peer_outside_pid_namespace"},
            )
        elif peer.uid not in (self._server_uid, 0):
            reason = "uid"
            reply = _failure(
                None,
                PEER_REFUSED,
                "the peer is not the server's user nor root",
                {"code": "peer_uid"},
            )
        if reason is None:
            return None
        structlog.get_logger().warning(
            "local_cli.peer_refused", reason=reason, uid=peer.uid, pid=peer.pid
        )
        return reply

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Counted from accept until the connection is closed, answer and drain included: a client
        # that keeps the connection open after its reply still holds one of the places.
        self._active += 1
        try:
            await self._serve_one(reader, writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            structlog.get_logger().exception("local_cli.connection_failed")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self._active -= 1

    async def _serve_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer = self._peer_reader(writer.get_extra_info("socket"))
        except OSError:
            structlog.get_logger().warning("local_cli.peer_refused", reason="unreadable")
            await self._reply_and_close(
                reader, writer, _failure(None, PEER_REFUSED, "unreadable peer")
            )
            return
        refusal = self._refusal(peer)
        if refusal is not None:
            await self._reply_and_close(reader, writer, refusal)
            return
        if self._active > self._max_connections:
            writer.write(_failure(None, BUSY, "too many connections"))  # no lingering: free now
            with contextlib.suppress(Exception):
                await writer.drain()
            return
        await self._reply_and_close(reader, writer, await self._exchange(reader, peer))

    async def _exchange(self, reader: asyncio.StreamReader, peer: Peer) -> bytes:
        try:
            line = await asyncio.wait_for(reader.readline(), self._read_timeout)
        except TimeoutError:
            return _failure(None, TIMEOUT, "no complete line in time")
        except (ValueError, asyncio.LimitOverrunError):
            return _failure(None, LINE_TOO_LONG, "line too long")
        try:
            request = json.loads(line)
        except (ValueError, RecursionError):  # not JSON, or nested deeper than the parser allows
            return _failure(None, PARSE_ERROR, "parse error")
        if not isinstance(request, dict) or not isinstance(request.get("method"), str):
            return _failure(None, INVALID_REQUEST, "invalid request")
        identifier = request.get("id")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return _failure(identifier, INVALID_PARAMS, "params must be an object")
        try:
            result = await asyncio.wait_for(
                self._handler(peer, request["method"], params), self._handler_timeout
            )
        except TimeoutError:
            return _failure(identifier, TIMEOUT, "the command took too long")
        except RpcError as error:
            return _failure(identifier, error.code, error.message, error.data)
        except Exception:
            structlog.get_logger().exception("local_cli.handler_failed", method=request["method"])
            return _failure(identifier, INTERNAL_ERROR, "internal error")
        return _response(identifier, result=result)

    async def _reply_and_close(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, reply: bytes
    ) -> None:
        """Answer, then read what the client still sends (bounded), so that closing with unread
        data does not reset the connection before the client has read the answer."""
        writer.write(reply)
        with contextlib.suppress(Exception):
            await writer.drain()
            writer.write_eof()
            async with asyncio.timeout(self._drain_deadline):  # a total bound, not per read
                drained = 0
                while drained < _DRAIN_BYTES:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    drained += len(chunk)
