"""DNS resolution, done by the Fetcher itself so that the answer it validates is the one it uses."""

from __future__ import annotations

import asyncio
import socket
from typing import Protocol

from shelfhold.infrastructure.fetcher.errors import DnsResolutionError


class Resolver(Protocol):
    async def resolve(self, host: str, port: int) -> list[str]: ...


class SystemResolver:
    """The operating system's resolver (getaddrinfo), off the event loop thread."""

    async def resolve(self, host: str, port: int) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise DnsResolutionError(f"cannot resolve {host!r}") from exc
        seen: dict[str, None] = {}
        for info in infos:
            seen[str(info[4][0]).split("%", 1)[0]] = None
        return list(seen)
