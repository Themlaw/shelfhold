"""The local CLI channel: JSON-RPC, one line per message, over a Unix socket (EF-USR-024).

The only way a process becomes the "local CLI" credential: never HTTP, never TCP, loopback
included (ADR-0049). Raw sockets are confined to this package (ruff TID251, import-linter).
"""

from shelfhold.infrastructure.local_socket.client import (
    RpcFailure,
    ServerUnavailable,
    UntrustedServer,
    call,
)
from shelfhold.infrastructure.local_socket.server import (
    SOCKET_NAME,
    Handler,
    LocalSocketServer,
    Peer,
    PeerReader,
    RpcError,
    read_peer,
)

__all__ = [
    "SOCKET_NAME",
    "Handler",
    "LocalSocketServer",
    "Peer",
    "PeerReader",
    "RpcError",
    "RpcFailure",
    "ServerUnavailable",
    "UntrustedServer",
    "call",
    "read_peer",
]
