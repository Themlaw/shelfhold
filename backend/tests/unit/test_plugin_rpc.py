"""M6 L04 (EF-SRC-016, D12): `rpc_protocol` 1, the wire between the core and an isolated plugin.

Length-prefixed JSON-RPC 2.0, a `hello` before the manifest, the plugin calling the host only
during a host call, bounds on size, time, memory and calls in flight, and a crash that marks the
plugin in error while the host goes on. The plugin side runs in a real subprocess; a hand-made
peer plays the plugin where the test needs to say exactly what is sent.
"""

from __future__ import annotations

import ast
import asyncio
import json
import socket
import struct
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from shelfhold_sdk.models import Cursor
from shelfhold_sdk.refs import SeriesRef

from shelfhold.infrastructure.fetcher import GuardedFetcher
from shelfhold.infrastructure.fetcher.client import identifiable_user_agent
from shelfhold.infrastructure.fetcher.guard import DomainAllowlist
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.plugin_host.adapters.native.openlibrary import build_manifest
from shelfhold.plugin_host.adapters.rpc.stdio import closed_environment, spawn_stdio
from shelfhold.plugin_host.provider_fetcher import ProviderFetcher
from shelfhold.plugin_host.registry import PluginOrigin, PluginRegistry, PluginStatus
from shelfhold.plugin_host.rpc import codec, protocol
from shelfhold.plugin_host.rpc.host_side import RpcSession
from shelfhold.plugin_host.rpc.protocol import Channel, RpcLimits
from tests.fakes_fetcher import FakeResolver, Recorder

FAKES = Path(__file__).resolve().parent.parent / "fakes"
RPC_PKG = Path(__file__).resolve().parents[2] / "src/shelfhold/plugin_host/rpc"
PUBLIC = "93.184.216.34"


def run[T](make: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(asyncio.wait_for(make(), 60))


def plugin_argv(entry: str = "rpc_plugin:make") -> list[str]:
    return [
        sys.executable, "-I", "-m", "shelfhold.plugin_host.rpc.plugin_side",
        "--path", str(FAKES), "--entry", entry,
    ]  # fmt: skip


async def open_real(
    limits: RpcLimits | None = None,
    entry: str = "rpc_plugin:make",
    key: str = "rpc-demo",
    **kw: Any,
) -> RpcSession:
    limits = limits or RpcLimits()
    channel = await spawn_stdio(plugin_argv(entry), limits=limits)
    return await RpcSession.open(channel, key=key, limits=limits, **kw)


def titles(page: Any) -> list[str]:
    return [item["title"] if isinstance(item, dict) else item.title for item in page.items]


def search(session: RpcSession, q: str, fetcher: Any = None, **kw: Any) -> Awaitable[Any]:
    return session.call(
        "source.search", {"q": q, "filters": kw.pop("filters", {}), "cursor": None},
        fetcher=fetcher, **kw,
    )  # fmt: skip


# -- a hand-made plugin, to say exactly what is on the wire --------------------------------------


class Peer:
    """The plugin end of a socket pair: reads what the host sends, writes what the test says."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.received: list[dict[str, Any]] = []

    async def read(self) -> dict[str, Any]:
        payload = await codec.read_frame(self.reader)
        assert payload is not None
        message: dict[str, Any] = codec.decode_payload(payload)
        self.received.append(message)
        return message

    async def send(self, message: dict[str, Any]) -> None:
        await codec.write_frame(self.writer, message)

    async def reply(self, request: dict[str, Any], result: Any) -> None:
        await self.send({"jsonrpc": "2.0", "id": request["id"], "result": result})


async def pair() -> tuple[Channel, Peer]:
    left, right = socket.socketpair()
    host_r, host_w = await asyncio.open_connection(sock=left)
    peer_r, peer_w = await asyncio.open_connection(sock=right)

    async def close() -> None:
        host_w.close()

    async def exit_reason() -> str | None:
        return None

    return Channel(host_r, host_w, close, exit_reason), Peer(peer_r, peer_w)


MANIFEST = {
    "key": "rpc-demo", "kind": "source", "version": "1.0.0", "api": 2, "rpc_protocol": 1,
    "name": "demo", "langs": ["en"], "content_kinds": ["text"], "url_patterns": [],
    "capabilities": ["SEARCH"], "rate_limit": {"requests_per_second": 1.0},
}  # fmt: skip


async def hand_made(answer_hello: Any = None) -> tuple[RpcSession, Peer]:
    """A session open on a hand-made plugin that agreed on protocol 1 and gave its manifest."""
    channel, peer = await pair()

    async def play() -> None:
        hello = await peer.read()
        await peer.reply(hello, {"rpc_protocol": 1} if answer_hello is None else answer_hello)
        manifest = await peer.read()
        await peer.reply(manifest, MANIFEST)

    opening = asyncio.ensure_future(play())
    session = await RpcSession.open(channel, key="rpc-demo", limits=RpcLimits())
    await opening
    return session, peer


# -- the seven tests of 03 -----------------------------------------------------------------------


@pytest.mark.req("EF-SRC-016")
def test_plugin_subprocess_jsonrpc_rpc_protocol_distinct_from_api() -> None:
    async def go() -> None:
        session = await open_real()
        try:
            # `rpc_protocol` was agreed by `hello`, before the manifest; `api` is the manifest's
            assert session.protocol == 1
            assert session.manifest["api"] == 2 and session.manifest["rpc_protocol"] == 1
            page = await search(session, "hello")
            assert titles(page) == ["echo:hello"]
        finally:
            await session.close()

    run(go)
    # ...and a different wire from the CLI socket: its own framing, no code of its own in common
    frame = codec.encode_frame({"jsonrpc": "2.0", "id": 1, "method": "hello"})
    assert struct.unpack(">I", frame[:4])[0] == len(frame) - 4 and not frame.endswith(b"\n")
    for source in RPC_PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any("local_socket" in n for n in names), source

    # `api` is negotiated apart from it: api 1 still loads (shim) over the same protocol 1
    async def registry() -> None:
        reg = PluginRegistry()

        async def connect() -> RpcSession:
            return await open_real()

        record = await reg.load_isolated("rpc-demo", PluginOrigin.THIRD_PARTY, connect)
        assert record.status is PluginStatus.LOADED and record.manifest is not None
        assert record.manifest.rpc_protocol == 1
        await record.instance.aclose()

    run(registry)


@pytest.mark.req("EF-SRC-016")
def test_isolated_plugin_crash_host_survives_plugin_marked_error() -> None:
    async def go() -> None:
        reg = PluginRegistry()
        good = await reg.load_isolated("rpc-demo", PluginOrigin.THIRD_PARTY, open_real)

        async def open_other() -> RpcSession:
            return await open_real(entry="rpc_plugin:make_other", key="rpc-demo-2")

        other = await reg.load_isolated("rpc-demo-2", PluginOrigin.THIRD_PARTY, open_other)
        assert good.status is PluginStatus.LOADED and other.status is PluginStatus.LOADED
        crashed = good.instance
        with pytest.raises(protocol.PluginFailed) as err:
            await crashed.search(None, "crash", {}, None)
        assert "exited with code 3" in str(err.value)
        record = reg.get("rpc-demo")
        assert record is not None and record.status is PluginStatus.ERROR
        assert "exited with code 3" in (record.reason or "")
        assert [r.key for r in reg.active()] == ["rpc-demo-2"]
        with pytest.raises(protocol.PluginFailed):  # an errored plugin is not called again
            await crashed.search(None, "again", {}, None)
        assert titles(await other.instance.search(None, "alive", {}, None)) == ["echo:alive"]
        await other.instance.aclose()

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_rpc_message_size_and_deadline_bounded() -> None:
    # a frame announcing more than 8 MiB is refused from its header, before any allocation
    async def too_big() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", codec.MAX_MESSAGE_BYTES + 1))
        with pytest.raises(codec.FrameTooLarge):
            await codec.read_frame(reader)

    run(too_big)
    with pytest.raises(codec.FrameTooLarge):
        codec.encode_frame({"x": "y" * codec.MAX_MESSAGE_BYTES})

    async def bounded() -> None:
        limits = RpcLimits(call_deadline=0.5, max_in_flight=4, cpu_seconds=2)
        session = await open_real(limits)
        try:
            # a result too large for a frame is the call's error, not the plugin's death
            with pytest.raises(protocol.RemoteError) as big:
                await search(session, "huge")
            assert big.value.code == protocol.RESULT_TOO_LARGE and session.state == "ready"
            # at most four calls are in flight at once, the others wait their turn
            peaks = await asyncio.gather(*(search(session, "peak", deadline=10) for _ in range(9)))
            assert max(int(titles(p)[0]) for p in peaks) <= 4
            # a call that outlives its deadline marks the plugin in error and stops it
            with pytest.raises(protocol.DeadlineExceeded):
                await search(session, "sleep")
            assert str(session.state) == "error" and "deadline" in (session.reason or "")
        finally:
            await session.close()

    run(bounded)

    async def cpu() -> None:
        session = await open_real(RpcLimits(call_deadline=30, cpu_seconds=1))
        try:
            with pytest.raises(protocol.PluginFailed) as err:
                await search(session, "spin")
            assert "cpu" in str(err.value).lower() and session.state == "error"
        finally:
            await session.close()

    run(cpu)

    async def memory() -> None:
        session = await open_real(RpcLimits(memory_bytes=256 << 20))
        try:
            with pytest.raises((protocol.RemoteError, protocol.PluginFailed)):
                await search(session, "memory")
        finally:
            await session.close()

    run(memory)


def sdk_fetcher(rec: Recorder, resolver: FakeResolver, *hosts: str) -> ProviderFetcher:
    from shelfhold_sdk.models import DomainRule

    rules = [DomainRule(pattern=h, purpose="api") for h in hosts]
    inner = GuardedFetcher(
        resolver, transport=rec.transport, user_agent=identifiable_user_agent(None),
        domains=DomainAllowlist("rpc-demo", rules),
    )  # fmt: skip
    return ProviderFetcher(inner, build_manifest())


class Leaky:
    """A Fetcher whose refusal quotes the credential it was holding."""

    def __init__(self, secret: str) -> None:
        self.secret = secret

    async def get(self, url: str, **kw: Any) -> Any:
        from shelfhold.infrastructure.fetcher import SsrfBlocked

        raise SsrfBlocked(f"refused {url}?key={self.secret} for {self.secret}")

    def settings(self) -> dict[str, Any]:
        return {}


@pytest.mark.req("EF-SRC-016", "ENF-SEC-001")
def test_rpc_fetcher_goes_through_ssrf_guard() -> None:
    secret = "tok-" + "9f3a7c1d2b8e4a60"  # generated for the test, never a real key
    register_secret_value(secret)
    rec = Recorder(
        lambda r: __import__("httpx2").Response(
            200, content=b"page", headers={"Set-Cookie": "sid=abc", "X-Plain": "1"}
        )
    )
    dns = FakeResolver(
        {"ok.example.org": [PUBLIC], "inner.example.org": ["10.0.0.5"], "out.example.org": [PUBLIC]}
    )

    async def go() -> None:
        fetcher = sdk_fetcher(rec, dns, "ok.example.org", "inner.example.org")
        session = await open_real()
        try:
            got = await search(
                session, "fetch", fetcher, filters={"url": "https://ok.example.org/p"}
            )
            status, body, names = titles(got)[0].split(":", 2)
            assert (status, body) == ("200", "page")
            assert "x-plain" in names and "set-cookie" not in names.lower()  # no session leaks
            before = len(rec.requests)
            for url, refused in (
                ("https://inner.example.org/", "SsrfBlocked"),  # resolves to a private address
                ("https://out.example.org/", "DomainNotAllowed"),  # not in the manifest's list
            ):
                page = await search(session, "fetch-error", fetcher, filters={"url": url})
                kind, _, text = titles(page)[0].partition("|")
                assert kind == refused and secret not in text
            assert len(rec.requests) == before  # nothing was sent for a refused request
            # a refusal that quotes a secret is scrubbed on the host's side before it is sent
            leaky = Leaky(secret)
            page = await search(
                session, "fetch-error", leaky, filters={"url": "https://ok.example.org/"}
            )
            kind, _, text = titles(page)[0].partition("|")
            assert kind == "SsrfBlocked" and secret not in text
            # a plugin that is handed no fetcher has none to call
            none = await search(
                session, "fetch-error", None, filters={"url": "https://ok.example.org/"}
            )
            assert "no_fetcher" in titles(none)[0] or "Fetcher" in titles(none)[0]
        finally:
            await session.close()

    run(go)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_rpc_never_carries_a_live_object() -> None:
    class Live:
        pass

    a, b = socket.socketpair()
    try:
        with open(__file__) as handle:
            for live in (Live(), lambda: 1, handle, a, asyncio.Lock(), {1, 2}, {1: "k"}):
                with pytest.raises(codec.LiveObjectError):
                    codec.encode_frame(codec.to_wire({"p": live}))
        assert codec.to_wire(b"\x00\xff") == "AP8="  # bytes travel as base64
        ref = SeriesRef(source_key="s", remote_id="1", title="t")
        assert codec.to_wire(Cursor(token="n")) == {"token": "n"}  # noqa: S106
        assert codec.to_wire([ref])[0]["remote_id"] == "1"
    finally:
        a.close()
        b.close()
    # strict decoding: NaN, duplicate keys, too deep
    for bad in (b'{"a": NaN}', b'{"a": 1, "a": 2}', b"[" * 70 + b"]" * 70, b"\xff", b"{"):
        with pytest.raises(codec.FrameError):
            codec.decode_payload(bad)

    async def go() -> None:
        session, peer = await hand_made()
        try:
            with pytest.raises(codec.LiveObjectError):  # refused before anything is sent
                await session.call("source.get_series", {"ref": Live()}, fetcher=None)
            sent = len(peer.received)
            assert sent == 2  # `hello` and `manifest`, nothing else
            # the fetcher the host hands a call is never an argument: no frame carries it
            task = asyncio.ensure_future(search(session, "x", sdk_fetcher_none()))
            request = await peer.read()
            assert set(request["params"]) <= {"call", "args", "context"}
            assert "f" not in request["params"]["args"]
            await peer.reply(request, {"items": [], "next": None})
            await task
        finally:
            await session.close()

    run(go)

    # the real plugin: a result that cannot be JSON is an error of that call
    async def real() -> None:
        session = await open_real()
        try:
            with pytest.raises(protocol.RemoteError):
                await search(session, "live")
        finally:
            await session.close()

    run(real)


def sdk_fetcher_none() -> Any:
    class Quiet:
        def settings(self) -> dict[str, Any]:
            return {"language": "en"}

        def cookies(self) -> Any:
            from shelfhold_sdk.fetcher import CookieJarView

            return CookieJarView()

    return Quiet()


@pytest.mark.req("EF-SRC-016")
def test_rpc_plugin_cannot_call_host_outside_a_host_call() -> None:
    class Spy:
        calls = 0

        async def get(self, *a: Any, **k: Any) -> Any:
            Spy.calls += 1

    async def idle() -> None:
        session, peer = await hand_made()
        try:
            await peer.send(
                {"jsonrpc": "2.0", "id": 7, "method": "fetcher.get",
                 "params": {"call": 1, "url": "https://ok.example.org/"}}
            )  # fmt: skip
            answer = await peer.read()
            assert answer["id"] == 7 and answer["error"]["code"] == protocol.OUTSIDE_HOST_CALL
            await asyncio.sleep(0.1)
            assert session.state == "error" and "outside a host call" in (session.reason or "")
        finally:
            await session.close()

    run(idle)

    async def after() -> None:
        session, peer = await hand_made()
        try:
            task = asyncio.ensure_future(search(session, "x", sdk_fetcher_none()))
            request = await peer.read()
            await peer.reply(request, {"items": [], "next": None})
            await task
            await peer.send(  # the call is over: its number no longer opens anything
                {"jsonrpc": "2.0", "id": 8, "method": "events.publish",
                 "params": {"call": request["id"], "name": "late"}}
            )  # fmt: skip
            answer = await peer.read()
            assert answer["error"]["code"] == protocol.OUTSIDE_HOST_CALL
        finally:
            await session.close()

    run(after)

    async def unknown() -> None:  # and the plugin may only name the four methods of the host
        session, peer = await hand_made()
        try:
            await peer.send({"jsonrpc": "2.0", "id": 9, "method": "source.search", "params": {}})
            assert (await peer.read())["error"]["code"] == protocol.METHOD_NOT_FOUND
            await asyncio.sleep(0.1)
            assert session.state == "error"
        finally:
            await session.close()

    run(unknown)
    assert Spy.calls == 0
    assert frozenset(
        {"fetcher.get", "fetcher.post", "fetcher.download", "events.publish"}
    ) == protocol.PLUGIN_TO_HOST_METHODS


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_rpc_protocol_out_of_range_refused_before_manifest() -> None:
    async def go() -> None:
        channel, peer = await pair()

        async def play() -> None:
            hello = await peer.read()
            assert hello["method"] == "hello" and hello["params"] == {"rpc_protocol": [1]}
            await peer.reply(hello, {"rpc_protocol": 2})  # a plugin of a future protocol

        playing = asyncio.ensure_future(play())
        with pytest.raises(protocol.ProtocolOutOfRange) as err:
            await RpcSession.open(channel, key="rpc-demo", limits=RpcLimits())
        await playing
        assert err.value.offered == 2 and err.value.supported == (1,)
        await asyncio.sleep(0.05)
        assert [m["method"] for m in peer.received] == ["hello"]  # the manifest was never asked

        # through the registry: an incompatible record, no manifest, nothing loaded
        channel2, peer2 = await pair()

        async def play2() -> None:
            hello = await peer2.read()
            await peer2.send(
                {"jsonrpc": "2.0", "id": hello["id"], "error": {"code": -32010, "message": "no"}}
            )

        playing2 = asyncio.ensure_future(play2())
        reg = PluginRegistry()

        async def connect() -> RpcSession:
            return await RpcSession.open(channel2, key="rpc-demo", limits=RpcLimits())

        record = await reg.load_isolated("rpc-demo", PluginOrigin.THIRD_PARTY, connect)
        await playing2
        assert record.status is PluginStatus.INCOMPATIBLE and record.manifest is None
        assert "rpc_protocol" in (record.reason or "") and reg.active() == []

    run(go)


# -- more of D12 ---------------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_rpc_manifest_naming_another_protocol_is_refused() -> None:
    async def go() -> None:
        reg = PluginRegistry()
        session, peer = await hand_made()
        session.manifest["rpc_protocol"] = 2  # the manifest disagrees with what was agreed

        async def connect() -> RpcSession:
            return session

        record = await reg.load_isolated("rpc-demo", PluginOrigin.THIRD_PARTY, connect)
        assert record.status is PluginStatus.INCOMPATIBLE
        assert "rpc_protocol" in (record.reason or "") and peer is not None

    run(go)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_rpc_carries_no_secret_in_messages_arguments_or_environment() -> None:
    secret = "key-" + "c41b8d7e5a90f263"
    register_secret_value(secret)

    async def go() -> None:
        session, peer = await hand_made()
        try:
            with pytest.raises(protocol.SecretLeak):
                await search(session, f"find {secret}", sdk_fetcher_none())
            assert len(peer.received) == 2  # no frame left for it
        finally:
            await session.close()

    run(go)
    env = closed_environment({"SHELFHOLD_SECRET_KEY": secret, "API_TOKEN": secret, "PATH": "/x"})
    assert not any(secret in v for v in env.values())
    assert not any(k.startswith("SHELFHOLD_") or "TOKEN" in k for k in env)

    async def real() -> None:
        session = await open_real()
        try:
            names = titles(await search(session, "env"))[0].split("|")
            assert not [n for n in names if n.startswith("SHELFHOLD_") or "SECRET" in n]
        finally:
            await session.close()

    run(real)


@pytest.mark.req("EF-SRC-016")
def test_rpc_hosts_calls_are_the_contract_methods_only() -> None:
    from shelfhold_sdk.models import PluginKind

    source = protocol.host_methods(PluginKind.SOURCE)
    assert {"source.search", "source.fetch_unit", "source.migrate_ref"} <= set(source)
    assert "export_target.scan_calls" in protocol.host_methods(PluginKind.EXPORT_TARGET)
    assert "metadata.identify" in protocol.host_methods(PluginKind.METADATA)
    assert protocol.host_methods(PluginKind.TRACKER) == {}  # the contract arrives with L13

    async def go() -> None:
        session, _peer = await hand_made()
        try:
            with pytest.raises(protocol.RpcError):  # not a method of the contract
                await session.call("source.__init__", {}, fetcher=None)
            with pytest.raises(protocol.RpcError):  # a plugin's own kind only
                await session.call("export_target.scan_calls", {}, fetcher=None)
        finally:
            await session.close()

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_rpc_frames_roundtrip_and_stream_in_pieces() -> None:
    frame = codec.encode_frame({"jsonrpc": "2.0", "id": 1, "result": {"b": "é"}})

    async def go() -> None:
        reader = asyncio.StreamReader()
        for i in range(0, len(frame), 3):  # a frame is never assumed to arrive whole
            reader.feed_data(frame[i : i + 3])
        reader.feed_eof()
        payload = await codec.read_frame(reader)
        assert payload is not None and codec.decode_payload(payload)["result"] == {"b": "é"}
        assert await codec.read_frame(reader) is None  # a clean end
        cut = asyncio.StreamReader()
        cut.feed_data(frame[:-2])
        cut.feed_eof()
        with pytest.raises(codec.FrameError):  # an end in the middle of a frame is not clean
            await codec.read_frame(cut)

    run(go)
    assert json.loads(frame[4:])["id"] == 1
