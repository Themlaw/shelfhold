"""The hub of Kavita as a transport for arrival events (EF-EXP-034, M4 L37; D11, D18).

Kavita announces `SeriesAdded` and `ScanSeries` on its SignalR hub; `events` knows what to make of
them (`HubSession`, `ArrivalEventWatcher`). This module only carries the bytes: it asks Kavita for
the token of its hub with the dedicated key, opens a WebSocket to the addresses the guard of the
fetcher validated, speaks the handshake and the pings of the protocol, and hands what it reads to
`HubSession`, then the events to `ArrivalEventWatcher.on_events`.

Four rules, each pinned by a test:

- **An event is a reason to look, never a proof.** Nothing here reads a state from a frame; what
  confirms an arrival is the API of Kavita, read by the poller as ever.
- **The polling stays on.** This transport is an addition: the export job still settles every
  arrival by itself, and it does not know whether a hub exists.
- **A hub that is down cuts only the transport.** Every failure of the hub, of its token, of its
  connection or of a look ends the attempt, never the task: the next attempt comes after a growing,
  bounded wait. Nothing is written to the health of the target, nothing is announced.
- **Neither the key nor the token is written down.** The URL (which carries the token) lives in a
  `HubEndpoint` whose repr is redacted, failures are logged by their class only, and the token is
  also registered for scrubbing by value.

A Kavita woken by a hook (Sablier) is not listened to: a connection that never closes would keep
it awake.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from ipaddress import IPv4Address, IPv6Address
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import structlog
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from shelfhold.export.targets.kavita.client import KavitaClient, hub_url
from shelfhold.export.targets.kavita.dto import HubEndpoint
from shelfhold.export.targets.kavita.errors import HubDown
from shelfhold.export.targets.kavita.events import ArrivalEventWatcher, HubSession
from shelfhold.infrastructure.fetcher import open_pinned_socket

__all__ = [
    "HubConnection",
    "HubConnector",
    "HubDown",
    "HubEndpoint",
    "HubListeners",
    "KavitaHubTransport",
    "WebSocketConnector",
    "hub_url",
]

log = structlog.get_logger(__name__)

# `websockets` logs the opening request at DEBUG, path and query included, so the token of the hub
# would reach any handler an admin sets to debug. Its connections are given this logger instead: it
# does not propagate, has no handler of its own and stops below WARNING, so nothing that names the
# URL is ever written, whatever the level of the root logger or of `websockets`.
_QUIET = logging.getLogger("shelfhold.kavita.hub.websockets")
_QUIET.propagate = False
_QUIET.setLevel(logging.WARNING)
_QUIET.addHandler(logging.NullHandler())

PING: Final = b'{"type": 6}\x1e'  # SignalR's keep-alive, which Kavita's server waits for
PING_EVERY: Final = 15.0  # seconds (Kavita's server drops a client silent for 30)
SILENCE_LIMIT: Final = 45.0  # the server pings every 15 s: three missed pings, the hub is gone
FIRST_WAIT: Final = 5.0
MAX_WAIT: Final = 300.0
MAX_MESSAGE: Final = 256 * 1024  # bytes: the frames above 64 KiB are dropped by `events` anyway
OPEN_TIMEOUT: Final = 10.0
RESCAN_EVERY: Final = 60.0


class HubConnection(Protocol):
    async def send(self, data: bytes) -> None: ...

    async def recv(self) -> bytes:
        """The next message, as bytes. `HubDown` when the connection is over."""
        ...

    async def close(self) -> None: ...


HubConnector = Callable[[HubEndpoint], Awaitable[HubConnection]]
OpenSocket = Callable[[IPv4Address | IPv6Address, int], Awaitable[Any]]  # a connected socket


# -- the real client (the `websockets` library, Python only, behind this one class) ----------------


class _WebSocket:
    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def send(self, data: bytes) -> None:
        try:
            # the JSON protocol of SignalR is text: a text frame, never a binary one
            await self._inner.send(data.decode())  # type: ignore[attr-defined]
        except (WebSocketException, OSError) as exc:
            raise HubDown(f"the hub connection failed ({type(exc).__name__})") from None

    async def recv(self) -> bytes:
        try:
            message = await self._inner.recv()  # type: ignore[attr-defined]
        except (WebSocketException, OSError) as exc:
            raise HubDown(f"the hub connection ended ({type(exc).__name__})") from None
        return message if isinstance(message, bytes) else message.encode()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._inner.close()  # type: ignore[attr-defined]


class WebSocketConnector:
    """Opens the hub over the addresses the guard validated, one after the other, and nothing else.

    The socket is made here, to an address of the endpoint, and handed to the library, which does
    the TLS (certificate and name verified, never relaxed) and the upgrade for the host of the URL:
    so the name validated is the name asked for, and no address is ever resolved a second time."""

    def __init__(
        self,
        *,
        open_socket: OpenSocket = open_pinned_socket,
        tls_context: ssl.SSLContext | None = None,
        open_timeout: float = OPEN_TIMEOUT,
    ) -> None:
        self._open_socket = open_socket
        self.tls_context = tls_context or ssl.create_default_context()
        self._open_timeout = open_timeout

    async def __call__(self, endpoint: HubEndpoint) -> HubConnection:
        parts = urlsplit(endpoint.url)
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        failure = "no address"
        for address in endpoint.addresses:
            try:
                async with asyncio.timeout(self._open_timeout):
                    sock = await self._open_socket(address, port)
            except (OSError, TimeoutError) as exc:
                failure = type(exc).__name__
                continue
            try:
                inner = await ws_connect(
                    endpoint.url,
                    sock=sock,
                    ssl=self.tls_context if parts.scheme == "wss" else None,
                    server_hostname=parts.hostname if parts.scheme == "wss" else None,
                    open_timeout=self._open_timeout,
                    logger=_QUIET,
                    max_size=MAX_MESSAGE,
                    max_queue=64,
                    proxy=None,  # never through a proxy of the environment: the guard chose
                    user_agent_header=None,
                    compression=None,
                )
            except (WebSocketException, OSError, TimeoutError) as exc:
                sock.close()
                failure = type(exc).__name__
                continue
            return _WebSocket(inner)
        raise HubDown(f"the hub could not be reached ({failure})")


# -- one connection, and the loop around it -----------------------------------------------


class KavitaHubTransport:
    """The transport of one Kavita target. `open_client` builds a client each time (a key rotated
    since the last attempt is read again)."""

    def __init__(
        self,
        open_client: Callable[[], KavitaClient],
        watcher: ArrivalEventWatcher,
        connect: HubConnector,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ping_every: float = PING_EVERY,
        silence_limit: float = SILENCE_LIMIT,
        first_wait: float = FIRST_WAIT,
        max_wait: float = MAX_WAIT,
    ) -> None:
        self._open_client = open_client
        self._watcher = watcher
        self._connect = connect
        self._sleep = sleep
        self._ping_every = ping_every
        self._silence_limit = silence_limit
        self._first_wait = first_wait
        self._max_wait = max_wait

    def __repr__(self) -> str:
        return "KavitaHubTransport(<redacted>)"

    async def run_once(self) -> None:
        """One connection, from the token to its end. It always ends by raising: `HubDown` when
        the hub is the reason, or whatever the failing part raised."""
        await self._attempt(await asyncio.to_thread(self._open_client))

    async def _serve(self, client: KavitaClient, connection: HubConnection) -> None:
        session = HubSession()
        await connection.send(session.handshake_request())
        silent = 0.0
        while True:
            try:
                data = await asyncio.wait_for(connection.recv(), self._ping_every)
            except TimeoutError:
                silent += self._ping_every
                if silent >= self._silence_limit:
                    raise HubDown("the hub is silent") from None
                await connection.send(PING)
                continue
            silent = 0.0
            events = session.receive(data)
            if session.closed:
                raise HubDown("the hub did not acknowledge the handshake")
            if not events:
                continue
            try:
                await self._watcher.on_events(client, events)
            except Exception as error:  # a failing look never ends the connection
                log.info("kavita.hub_look_failed", reason=type(error).__name__)

    async def run(self) -> None:
        """Listen until cancelled. Only the cancellation leaves this method."""
        wait = self._first_wait
        while True:
            try:
                client = await asyncio.to_thread(self._open_client)
            except KeyError:  # not a Kavita target (a Komga, a folder): nothing to listen to
                return
            except Exception as error:
                log.warning("kavita.hub_down", reason=type(error).__name__)
                await self._sleep(wait)
                wait = min(wait * 2, self._max_wait)
                continue
            if client.wake_managed:
                log.info("kavita.hub_skipped", reason="woken_by_hook")
                return
            try:
                await self._attempt(client)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # hub, token, socket or look: the polling goes on
                log.warning("kavita.hub_down", reason=type(error).__name__)
            await self._sleep(wait)
            wait = min(wait * 2, self._max_wait)

    async def _attempt(self, client: KavitaClient) -> None:
        endpoint = await client.hub_endpoint()
        connection = await self._connect(endpoint)
        try:
            await self._serve(client, connection)
        finally:
            await connection.close()


# -- one transport per Kavita target -----------------------------------------------


class _Runnable(Protocol):
    async def run(self) -> None: ...


class HubListeners:
    """Keeps one transport running for each Kavita target, only while this instance owns the data
    (a second instance, waiting for a migration's lock, listens to nothing). A target added later is
    picked up at the next look; a transport that ends on its own is not restarted: it either has
    nothing to listen to or it already says so in the log."""

    def __init__(
        self,
        targets: Callable[[], Sequence[str]],
        transport_for: Callable[[str], _Runnable],
        *,
        active: Callable[[], bool],
    ) -> None:
        self._targets = targets
        self._transport_for = transport_for
        self._active = active
        self._started: dict[str, asyncio.Task[None]] = {}

    async def _reconcile(self) -> None:
        if not self._active():
            return
        try:
            wanted = await asyncio.to_thread(self._targets)
        except Exception as error:
            log.warning("kavita.hub_targets_unread", reason=type(error).__name__)
            return
        for target_id in wanted:
            if target_id not in self._started:
                self._started[target_id] = asyncio.ensure_future(self._guarded(target_id))

    async def _guarded(self, target_id: str) -> None:
        try:
            await self._transport_for(target_id).run()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # one broken transport stops no other
            log.warning("kavita.hub_stopped", reason=type(error).__name__)

    @contextlib.asynccontextmanager
    async def running(self, *, rescan_every: float = RESCAN_EVERY) -> AsyncIterator[None]:
        async def watch() -> None:
            while True:
                await self._reconcile()
                await asyncio.sleep(rescan_every)

        watcher = asyncio.ensure_future(watch())
        try:
            yield
        finally:
            tasks = [watcher, *self._started.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._started.clear()
