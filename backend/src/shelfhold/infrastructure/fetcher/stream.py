"""A connected socket to an address the guard validated, for a client that must hold a connection
open (the hub of Kavita, M4 L37) and so cannot go through a request/response `get`.

Raw sockets are the Fetcher's alone (ENF-SEC-001): this is where one is made. It resolves nothing
(the address comes from `GuardedFetcher.check_destination`, which is the verdict), so there is no
second resolution to rebind, and it is handed to the library that speaks the protocol.
"""

from __future__ import annotations

import asyncio
import socket
from ipaddress import IPv4Address, IPv6Address

__all__ = ["open_pinned_socket"]


async def open_pinned_socket(
    address: IPv4Address | IPv6Address, port: int, *, timeout: float = 10.0
) -> socket.socket:
    """A non-blocking socket connected to exactly this address and port, or the error."""
    family = socket.AF_INET6 if isinstance(address, IPv6Address) else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        async with asyncio.timeout(timeout):
            await asyncio.get_running_loop().sock_connect(sock, (str(address), port))
    except BaseException:
        sock.close()
        raise
    return sock
