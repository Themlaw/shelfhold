"""M4 L37 — EF-EXP-034 (D11, D18): the hub of Kavita as a transport for arrival events.

The transport only hands what the hub says to `HubSession` and the events to
`ArrivalEventWatcher.on_events`. Every "never" of the card is a test that tries to break it: an
event never confirms an arrival, the polling goes on whatever the hub does, a hub that is down cuts
nothing but the transport, and neither the key nor the token of the hub is ever written down.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import structlog

from shelfhold.export.targets.kavita.events import ArrivalEventWatcher
from shelfhold.export.targets.kavita.events_transport import (
    HubDown,
    HubEndpoint,
    HubListeners,
    KavitaHubTransport,
    WebSocketConnector,
    hub_url,
)
from shelfhold.export.targets.kavita.polling import ArrivalPoller
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv, export_env
from tests.fakes.kavita import API_KEY, BASE_URL
from tests.fakes.kavita.hub import ACK, FakeHub, frame
from tests.kavita_env import run
from tests.kavita_export_env import REMOTE_ROOT, Wired, wire

pytestmark = pytest.mark.req("EF-EXP-034")


class Sleeps:
    """The injected sleep: nothing waits, and the n-th call stops the transport."""

    def __init__(self, stop_after: int) -> None:
        self.delays: list[float] = []
        self._stop_after = stop_after

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self._stop_after:
            raise asyncio.CancelledError


def _transport(w: Wired, hub: FakeHub, **options: Any) -> KavitaHubTransport:
    watcher = ArrivalEventWatcher(w.env.clock, ArrivalPoller(w.env.engine, w.env.clock, w.events))
    return KavitaHubTransport(lambda: w.client, watcher, hub, **options)


def _state(env: ExportEnv) -> str:
    state: str = env.rows(export.export_record)[0].state
    return state


def _ready(w: Wired) -> None:
    (rel,) = w.written()
    folder = f"{REMOTE_ROOT}/{rel}".rsplit("/", 1)[0]
    w.fake.index_after_scans("Dr Stone", folder, [w.remote(rel)])
    run(w.client.scan_folder(folder))  # Kavita scanned: the event tells so, the API holds it


def test_hub_transport_feeds_arrival_watcher() -> None:
    with export_env() as env:
        w = wire(env)
        _ready(w)
        hub = FakeHub([[ACK, frame("SeriesAdded", seriesId=1)]])
        with pytest.raises(HubDown):  # the script ends: the hub closes the connection
            run(_transport(w, hub).run_once())
        # the arrival is confirmed from what the API of Kavita holds, after the event
        assert _state(env) == "resolved"
        assert env.rows(export.export_record)[0].remote_ref == str(w.fake.series[0]["id"])
        (connection,) = hub.connections
        assert connection.sent[0] == b'{"protocol": "json", "version": 1}\x1e'  # the handshake
        assert connection.closed
        # the hub was reached with the token Kavita handed out for the dedicated key
        (endpoint,) = hub.endpoints
        (token,) = w.fake.hub_tokens
        assert endpoint.url == hub_url(BASE_URL, token)
        assert endpoint.url.startswith("ws://kavita.lan:5000/hubs/messages?access_token=")
        (call,) = [r for r in w.fake.requests if r.path.endswith("/Plugin/authenticate")]
        assert call.method == "POST" and call.query["apiKey"] == API_KEY
        assert w.fake.violations == []


def test_hub_url_follows_the_scheme_and_keeps_the_sub_path() -> None:
    assert hub_url("https://kavita.lan", "t") == "wss://kavita.lan/hubs/messages?access_token=t"
    assert hub_url("http://kavita.lan:5000/", "t") == (
        "ws://kavita.lan:5000/hubs/messages?access_token=t"
    )
    assert hub_url("https://lan/kavita", "a b") == (
        "wss://lan/kavita/hubs/messages?access_token=a%20b"
    )


def test_hub_down_keeps_polling() -> None:
    with export_env() as env:
        w = wire(env)
        _ready(w)
        hub = FakeHub()  # every attempt: connection refused
        sleeps = Sleeps(stop_after=4)
        with pytest.raises(asyncio.CancelledError):
            run(_transport(w, hub, sleep=sleeps).run())  # only the cancellation leaves `run`
        assert len(hub.endpoints) == 4  # it keeps trying, with a growing wait, bounded
        assert sleeps.delays == sorted(sleeps.delays) and sleeps.delays[-1] > sleeps.delays[0]
        assert max(sleeps.delays) <= 300
        # nothing else was cut: the polling alone confirms, as before the hub existed
        poller = ArrivalPoller(env.engine, env.clock, w.events)
        assert run(poller.poll(w.client, env.destination_id)).indexed != []
        assert _state(env) == "resolved"
        assert not w.client.health.paused and w.events.codes() == []  # no target.* from the hub


def test_hub_that_drops_mid_stream_is_reconnected_and_keeps_serving() -> None:
    with export_env() as env:
        w = wire(env)
        _ready(w)
        hub = FakeHub([[ACK, OSError("synthetic reset")], [ACK, frame("SeriesAdded")]])
        with pytest.raises(asyncio.CancelledError):
            run(_transport(w, hub, sleep=Sleeps(stop_after=3)).run())
        assert len(hub.endpoints) == 3  # reset, then served, then closed, then refused
        assert _state(env) == "resolved"  # the event of the second connection was served


@pytest.mark.parametrize("status", [401, 404, 500])
def test_hub_authentication_refused_cuts_only_the_transport(status: int) -> None:
    with export_env() as env:
        w = wire(env)
        w.fake.hub_auth_status = status
        hub = FakeHub([[ACK]])
        with pytest.raises(asyncio.CancelledError):
            run(_transport(w, hub, sleep=Sleeps(stop_after=2)).run())
        assert hub.endpoints == []  # no token, no connection
        # a refusal of this route says nothing of the key: the health is not touched
        assert not w.client.health.paused and w.client.health.missing_routes == ()
        assert w.events.codes() == []


def test_hub_event_never_confirms_alone() -> None:
    with export_env() as env:
        w = wire(env)
        w.written()  # written, never scanned, never indexed by Kavita
        body: dict[str, Any] = {
            "state": "resolved",
            "remoteRef": "1",
            "folderPath": REMOTE_ROOT,
            "seriesId": 999,
        }
        hub = FakeHub([[ACK, frame("SeriesAdded", **body), frame("ScanSeries", **body)]])
        with pytest.raises(HubDown):
            run(_transport(w, hub).run_once())
        assert _state(env) == "written"  # a reason to look, never the proof
        assert env.rows(export.export_record)[0].remote_ref is None
        assert w.events.codes() == []
        assert w.fake.violations == []


def test_nothing_is_read_as_an_event_before_the_handshake_is_acknowledged() -> None:
    with export_env() as env:
        w = wire(env)
        _ready(w)
        before = len(w.fake.requests)
        hub = FakeHub([[frame("SeriesAdded"), ACK]])  # an event where the answer should be
        with pytest.raises(HubDown):
            run(_transport(w, hub).run_once())
        assert _state(env) == "written" and hub.connections[0].closed
        asked = [r.path for r in w.fake.requests[before:]]
        assert asked == ["/api/Plugin/authenticate"]  # the token, and not even a look


def test_a_refused_handshake_closes_the_connection() -> None:
    with export_env() as env:
        w = wire(env)
        hub = FakeHub([[b'{"error":"synthetic"}\x1e', frame("SeriesAdded")]])
        with pytest.raises(HubDown):
            run(_transport(w, hub).run_once())
        assert hub.connections[0].closed


def test_a_flood_of_events_makes_one_look_per_interval() -> None:
    with export_env() as env:
        w = wire(env)
        w.written()
        hub = FakeHub([[ACK, *[frame("SeriesAdded")] * 40]])
        with pytest.raises(HubDown):
            run(_transport(w, hub).run_once())
        looks = [r for r in w.fake.requests if r.path.endswith("/Series/all-v2")]
        assert len(looks) <= 2  # one look for the destination, whatever the number of events


def test_a_failing_look_does_not_end_the_connection() -> None:
    with export_env() as env:
        w = wire(env)
        _ready(w)

        def after_token(request: Any) -> None:  # Kavita goes away once the token is handed out
            if request.path.endswith("/Plugin/authenticate"):
                w.fake.asleep = True

        w.fake.hook = after_token  # the look itself then fails (Kavita unreachable)
        hub = FakeHub([[ACK, frame("SeriesAdded"), frame("ScanSeries")]])
        with pytest.raises(HubDown) as stopped:
            run(_transport(w, hub).run_once())
        assert "closed the connection" in str(stopped.value)  # the end of the script, not a look


def test_the_hub_is_kept_alive_with_the_pings_of_the_protocol() -> None:
    with export_env() as env:
        w = wire(env)
        hub = FakeHub([[ACK]], silent=True)  # the hub says nothing after the handshake
        transport = _transport(w, hub, ping_every=0.01, silence_limit=0.05)
        with pytest.raises(HubDown, match="silent"):
            run(transport.run_once())
        connection = hub.connections[0]
        assert connection.sent[0].startswith(b'{"protocol"')
        pings = [m for m in connection.sent[1:] if m == b'{"type": 6}\x1e']
        assert len(pings) >= 2  # silence from the hub does not stop the client's own pings
        assert connection.closed


def test_neither_the_key_nor_the_token_is_logged() -> None:
    with export_env() as env:
        w = wire(env)
        _ready(w)
        hub = FakeHub([[ACK, frame("SeriesAdded"), OSError("synthetic reset")], ConnectionError()])
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(asyncio.CancelledError),
        ):
            run(_transport(w, hub, sleep=Sleeps(stop_after=3)).run())
        assert len(w.fake.hub_tokens) == 3
        text = repr(logs) + repr(hub.endpoints) + repr(_transport(w, hub))
        assert API_KEY not in text
        assert all(token not in text for token in w.fake.hub_tokens)
        assert any(entry["event"] == "kavita.hub_down" for entry in logs)  # it said so, safely


def test_a_kavita_woken_by_a_hook_gets_no_standing_connection() -> None:
    """A connection that never closes would keep a sleeping Kavita awake (EF-INT-022)."""
    with export_env() as env:
        w = wire(env)

        async def wake() -> None: ...

        w.client._wake = wake
        hub = FakeHub([[ACK]])
        run(_transport(w, hub).run())  # returns at once: nothing to listen to
        assert hub.endpoints == [] and w.fake.hub_auth_calls == 0


# -- the supervisor: one transport per Kavita target, only on the instance that owns the data ----


class Spy:
    def __init__(self, name: str, failing: bool = False) -> None:
        self.name, self.failing, self.started, self.stopped = name, failing, False, False

    async def run(self) -> None:
        self.started = True
        try:
            if self.failing:
                raise RuntimeError("synthetic")
            await asyncio.Event().wait()
        finally:
            self.stopped = True


def test_listeners_run_one_transport_per_target_and_stop_with_the_app() -> None:
    spies = {name: Spy(name, failing=name == "b") for name in ("a", "b")}
    targets = ["a", "b"]

    async def scenario() -> None:
        listeners = HubListeners(lambda: list(targets), spies.__getitem__, active=lambda: True)
        async with listeners.running(rescan_every=0.01):
            await asyncio.sleep(0.05)
            assert spies["a"].started and spies["b"].started  # one broken one stops no other
            assert not spies["a"].stopped
            targets.append("c")
            spies["c"] = Spy("c")
            await asyncio.sleep(0.05)
            assert spies["c"].started  # a target added later is picked up
        assert spies["a"].stopped and spies["c"].stopped  # and everything stops with the app

    run(scenario())


def test_listeners_wait_while_the_instance_does_not_own_the_data() -> None:
    spies = {"a": Spy("a")}
    owns = False

    async def scenario() -> None:
        listeners = HubListeners(lambda: ["a"], spies.__getitem__, active=lambda: owns)
        async with listeners.running(rescan_every=0.01):
            await asyncio.sleep(0.05)
            assert not spies["a"].started

    run(scenario())


# -- the real WebSocket client, over a local socket pair: no network --------------------------


def test_websocket_connector_reaches_the_hub_through_the_pinned_socket(tmp_path: Path) -> None:
    import socket

    from websockets.asyncio.server import unix_serve

    seen: dict[str, Any] = {}

    async def hub(connection: Any) -> None:
        seen["path"] = connection.request.path
        seen["host"] = connection.request.headers["Host"]
        seen["handshake"] = await connection.recv()
        await connection.send(ACK.decode())
        await connection.send(frame("SeriesAdded", seriesId=3).decode())
        await asyncio.sleep(0.2)

    async def scenario() -> list[bytes]:
        path = str(tmp_path / "hub.sock")
        async with unix_serve(hub, path):
            opened: list[tuple[str, int]] = []

            async def open_socket(address: object, port: int) -> socket.socket:
                opened.append((str(address), port))
                loop = asyncio.get_running_loop()
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.setblocking(False)
                await loop.sock_connect(sock, path)
                return sock

            endpoint = HubEndpoint(
                url="ws://kavita.lan:5000/hubs/messages?access_token=synthetic",
                addresses=(__import__("ipaddress").ip_address("192.168.1.50"),),
            )
            connection = await WebSocketConnector(open_socket=open_socket)(endpoint)
            try:
                await connection.send(b'{"protocol": "json", "version": 1}\x1e')
                got = [await connection.recv(), await connection.recv()]
            finally:
                await connection.close()
            assert opened == [("192.168.1.50", 5000)]  # only the address the guard validated
            return got

    got = run(scenario())
    assert got[0] == ACK and got[1] == frame("SeriesAdded", seriesId=3)  # bytes, whatever the type
    assert seen["path"] == "/hubs/messages?access_token=synthetic"
    assert seen["host"] == "kavita.lan:5000"  # the name that was validated, not an address
    assert seen["handshake"] == '{"protocol": "json", "version": 1}\x1e'  # a text frame


def test_websocket_connector_turns_every_failure_into_hub_down() -> None:
    async def refused(address: object, port: int) -> Any:
        raise ConnectionRefusedError("synthetic")

    endpoint = HubEndpoint(
        url="ws://kavita.lan:5000/hubs/messages?access_token=synthetic",
        addresses=(__import__("ipaddress").ip_address("192.168.1.50"),),
    )
    with pytest.raises(HubDown) as down:
        run(WebSocketConnector(open_socket=refused)(endpoint))
    assert "synthetic" not in str(down.value) and "access_token" not in str(down.value)


def test_websocket_connector_refuses_a_plain_scheme_downgrade_of_tls() -> None:
    """A `wss` endpoint is verified (certificate and name), never relaxed."""
    connector = WebSocketConnector()
    context = connector.tls_context
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


# -- the failures of the token and of the socket: each one is a hub that is down -------------------


@pytest.mark.parametrize("body", ["not json", "[]", '{"token": ""}', '{"token": 5}'])
def test_an_answer_without_a_token_is_a_hub_down(body: str) -> None:
    with export_env() as env:
        w = wire(env)
        w.fake.hub_auth_body = body
        with pytest.raises(HubDown, match="not in the answer"):
            run(w.client.hub_endpoint())


def test_a_paused_target_or_an_unreachable_kavita_gives_no_endpoint() -> None:
    with export_env() as env:
        w = wire(env)
        w.client.health.paused = True
        with pytest.raises(HubDown, match="paused"):
            run(w.client.hub_endpoint())
        assert w.fake.hub_auth_calls == 0
        w.client.health.paused = False
        w.fake.asleep = True
        with pytest.raises(HubDown, match="could not be asked"):
            run(w.client.hub_endpoint())


def test_an_address_the_guard_refuses_gives_no_endpoint() -> None:
    from shelfhold.infrastructure.fetcher import SsrfBlocked

    with export_env() as env:
        w = wire(env)

        real = w.client._fetcher.check_destination
        calls: list[int] = []

        async def refused_the_second_time(host: str, port: int, **kwargs: Any) -> list[Any]:
            calls.append(1)
            if len(calls) > 1:  # the first look is the fetcher's own, for the token request
                raise SsrfBlocked("synthetic")
            return await real(host, port, **kwargs)  # type: ignore[no-any-return]

        w.client._fetcher.check_destination = refused_the_second_time  # type: ignore[method-assign]
        with pytest.raises(HubDown, match="address of the hub is refused"):
            run(w.client.hub_endpoint())


def test_a_target_that_is_not_a_kavita_is_not_listened_to() -> None:
    def not_kavita() -> Any:
        raise KeyError("a komga")

    run(KavitaHubTransport(not_kavita, None, FakeHub()).run())  # type: ignore[arg-type]


def test_a_client_that_cannot_be_built_is_retried_later() -> None:
    with export_env() as env:
        w = wire(env)
        built: list[int] = []

        def unreadable() -> Any:
            built.append(1)
            raise RuntimeError("the key is not readable")

        sleeps = Sleeps(stop_after=3)
        transport = KavitaHubTransport(unreadable, None, FakeHub(), sleep=sleeps)  # type: ignore[arg-type]
        with pytest.raises(asyncio.CancelledError):
            run(transport.run())
        assert len(built) == 3 and sleeps.delays == [5.0, 10.0, 20.0]
        assert w.events.codes() == []


def test_open_pinned_socket_connects_to_the_address_given_and_nothing_else() -> None:
    import ipaddress

    from shelfhold.infrastructure.fetcher import open_pinned_socket

    async def scenario() -> None:
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            sock = await open_pinned_socket(ipaddress.ip_address("127.0.0.1"), port)
            assert sock.getpeername() == ("127.0.0.1", port) and sock.getblocking() is False
            sock.close()
        with pytest.raises(OSError):  # nothing listens any more: the socket is closed, not leaked
            await open_pinned_socket(ipaddress.ip_address("127.0.0.1"), port, timeout=2)

    run(scenario())


def test_websocket_connector_reports_a_refused_upgrade_and_a_closed_connection(
    tmp_path: Path,
) -> None:
    import ipaddress
    import socket

    from websockets.asyncio.server import unix_serve
    from websockets.http11 import Response

    async def closing(connection: Any) -> None:
        await connection.send(ACK.decode())
        await connection.close()

    def refuse(connection: Any, request: Any) -> Any:
        if "refuse" in request.path:
            return Response(403, "Forbidden", connection.protocol.reject(403, "no").headers)
        return None

    async def scenario() -> None:
        path = str(tmp_path / "hub.sock")
        async with unix_serve(closing, path, process_request=refuse):

            async def open_socket(address: object, port: int) -> socket.socket:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.setblocking(False)
                await asyncio.get_running_loop().sock_connect(sock, path)
                return sock

            address = (ipaddress.ip_address("192.168.1.50"),)
            connector = WebSocketConnector(open_socket=open_socket)
            with pytest.raises(HubDown, match="could not be reached"):
                await connector(HubEndpoint("ws://kavita.lan:5000/refuse?access_token=t", address))
            connection = await connector(
                HubEndpoint("ws://kavita.lan:5000/hubs/messages?access_token=t", address)
            )
            assert await connection.recv() == ACK
            with pytest.raises(HubDown, match="ended"):  # the hub closed: not a crash
                await connection.recv()
            with pytest.raises(HubDown, match="failed"):
                await connection.send(b"x")
            await connection.close()

    run(scenario())


# -- the library's own logging: the URL of the hub carries the token ------------------------------


def _debug_everything() -> tuple[list[logging.LogRecord], Callable[[], None]]:
    """The stdlib logging as an admin would set it for a bug hunt: root and `websockets` at DEBUG,
    every record kept (formatted too, as a file or a console handler would write it)."""
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            record.getMessage()
            records.append(record)

    handler, root = Keep(level=logging.DEBUG), logging.getLogger()
    named = [logging.getLogger(n) for n in ("websockets", "websockets.client", "shelfhold")]
    before = [(logger, logger.level) for logger in (root, *named)]
    root.addHandler(handler)
    for logger in (root, *named):
        logger.setLevel(logging.DEBUG)

    def restore() -> None:
        root.removeHandler(handler)
        for logger, level in before:
            logger.setLevel(level)

    return records, restore


def _hub_log_records(tmp_path: Path, token: str) -> list[logging.LogRecord]:
    """Open a real `websockets` connection to a local synthetic hub (a Unix socket: no network)
    with the stdlib logging at DEBUG, and return what the client side wrote. The synthetic server
    is given a logger of its own that goes nowhere: Kavita's server is not this library."""
    import ipaddress
    import socket

    from websockets.asyncio.server import unix_serve

    deaf = logging.Logger("synthetic.hub.server")
    deaf.addHandler(logging.NullHandler())

    async def hub(connection: Any) -> None:
        await connection.recv()
        await connection.send(ACK.decode())
        await asyncio.sleep(0.1)

    async def scenario() -> None:
        path = str(tmp_path / "hub.sock")
        async with unix_serve(hub, path, logger=deaf):

            async def open_socket(address: object, port: int) -> socket.socket:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.setblocking(False)
                await asyncio.get_running_loop().sock_connect(sock, path)
                return sock

            endpoint = HubEndpoint(
                f"ws://kavita.lan:5000/hubs/messages?access_token={token}",
                (ipaddress.ip_address("192.168.1.50"),),
            )
            connection = await WebSocketConnector(open_socket=open_socket)(endpoint)
            await connection.send(b'{"protocol": "json", "version": 1}\x1e')
            assert await connection.recv() == ACK
            await connection.close()

    records, restore = _debug_everything()
    try:
        run(scenario())
    finally:
        restore()
    return records


def _leaking(records: list[logging.LogRecord], *secrets_: str) -> list[str]:
    return sorted(
        {r.name for r in records if any(s in r.getMessage() or s in repr(r.args) for s in secrets_)}
    )


def test_a_real_connection_never_writes_the_token_to_the_stdlib_logs(tmp_path: Path) -> None:
    token = secrets.token_urlsafe(18)
    records = _hub_log_records(tmp_path, token)
    assert _leaking(records, token, API_KEY) == []  # the client of the hub is silent


def test_the_stdlib_log_check_has_teeth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the library's default logger, the same connection does write the token at DEBUG."""
    from shelfhold.export.targets.kavita import events_transport

    monkeypatch.setattr(events_transport, "_QUIET", logging.getLogger("websockets.client"))
    token = secrets.token_urlsafe(18)
    assert _leaking(_hub_log_records(tmp_path, token), token) == ["websockets.client"]


def test_the_library_exceptions_that_name_the_url_never_reach_a_message_or_a_log() -> None:
    """`InvalidURI` carries the URI in its text, `InvalidStatus` does not: only the class goes."""
    import ipaddress
    import socket

    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus, InvalidURI
    from websockets.http11 import Response

    token = secrets.token_urlsafe(18)
    url = f"ws://kavita.lan:5000/hubs/messages?access_token={token}#fragment"  # fragment
    assert token in str(InvalidURI(url, "synthetic"))  # why only the class is kept
    assert token not in str(InvalidStatus(Response(403, "Forbidden", Headers(), b"")))

    async def open_socket(address: object, port: int) -> socket.socket:
        left, right = socket.socketpair()
        right.close()
        left.setblocking(False)
        return left

    endpoint = HubEndpoint(url, (ipaddress.ip_address("192.168.1.50"),))
    records, restore = _debug_everything()
    try:
        with structlog.testing.capture_logs() as logs, pytest.raises(HubDown) as down:
            run(WebSocketConnector(open_socket=open_socket)(endpoint))
    finally:
        restore()
    assert "InvalidURI" in str(down.value)  # the library did raise it: the url was in its text
    written = repr(logs) + " ".join(r.getMessage() for r in records)
    assert token not in str(down.value) and token not in written
    assert down.value.__cause__ is None and down.value.__context__ is None  # no chained text
