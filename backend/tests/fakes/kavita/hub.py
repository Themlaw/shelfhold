"""A synthetic hub of Kavita for the transport tests: scripted frames, no socket, no network."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from shelfhold.export.targets.kavita.events_transport import HubDown, HubEndpoint

RS = "\x1e"  # the record separator of the SignalR JSON protocol
ACK = ("{}" + RS).encode()  # what Kavita answers to an accepted handshake


def frame(target: str, library_id: int = 7, **body: object) -> bytes:
    message = {
        "type": 1,
        "target": target,
        "arguments": [{"name": target, "body": {"libraryId": library_id, **body}}],
    }
    return (json.dumps(message) + RS).encode()


@dataclass
class FakeHubConnection:
    """What the hub says, in order. An `Exception` in the script is raised when read; when the
    script is spent the hub closes the connection."""

    script: list[bytes | Exception]
    sent: list[bytes] = field(default_factory=list)
    closed: bool = False
    silent: bool = False  # once the script is spent, reads never return

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> bytes:
        if not self.script:
            if self.silent:  # a hub that stopped talking: the read never returns
                await asyncio.Event().wait()
            raise HubDown("the hub closed the connection")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeHub:
    """The connector the transport is given: one scripted connection per attempt. An attempt
    past the scripts, or one scripted as an exception, is a hub that is down."""

    attempts: Sequence[Sequence[bytes | Exception] | Exception] = ()
    endpoints: list[HubEndpoint] = field(default_factory=list)
    connections: list[FakeHubConnection] = field(default_factory=list)
    silent: bool = False  # the connections accepted say nothing

    async def __call__(self, endpoint: HubEndpoint) -> FakeHubConnection:
        self.endpoints.append(endpoint)
        index = len(self.endpoints) - 1
        if index >= len(self.attempts):
            raise ConnectionRefusedError("synthetic: the hub is down")
        attempt = self.attempts[index]
        if isinstance(attempt, Exception):
            raise attempt
        connection = FakeHubConnection(list(attempt), silent=self.silent)
        self.connections.append(connection)
        return connection
