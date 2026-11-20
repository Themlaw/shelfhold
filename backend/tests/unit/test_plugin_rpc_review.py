"""M6 L04, relecture adverse: what a hostile plugin can still do across `rpc_protocol` 1.

Each test is a proof of a defect found in review; it is red until the defect is fixed. The plugin
end is played by hand (`Peer`) or by `plugin_side.serve` on a socket pair, so that the test says
exactly what crosses the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import struct
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from shelfhold_sdk.fetcher import CookieJarView, DownloadedFile, Response
from shelfhold_sdk.models import Page
from shelfhold_sdk.refs import ImageRequest, SeriesRef

from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.plugin_host.adapters.rpc.stdio import spawn_stdio
from shelfhold.plugin_host.rpc import protocol
from shelfhold.plugin_host.rpc.host_side import RpcSession
from shelfhold.plugin_host.rpc.protocol import RpcLimits
from tests.unit.test_plugin_rpc import MANIFEST, hand_made, pair, run, search
from tests.unit.test_plugin_rpc_serve import Plugin, connect


async def _settle(session: RpcSession, peer: Any = None) -> None:
    """Free a session whatever state the test left it in, without hanging the test."""
    if peer is not None:
        peer.writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(session.close(), 5)


@pytest.mark.req("EF-SRC-016")
def test_review_rpc_deadline_covers_a_request_the_plugin_never_reads() -> None:
    """A plugin that stops reading its input: the host's write waits on `drain()` before the
    deadline starts, so the call never ends, its slot is never given back, and the plugin is
    never marked in error. `close()` then hangs as well, on the `shutdown` it sends."""

    async def go() -> None:
        session, peer = await hand_made()
        try:
            big = "x" * (4 << 20)  # past the socket's buffers: the writer has to wait
            with pytest.raises(protocol.PluginFailed):
                await asyncio.wait_for(search(session, big, deadline=0.5), 5)
            assert session.state == "error"
        finally:
            await _settle(session, peer)

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_review_rpc_close_is_bounded_when_the_plugin_never_reads() -> None:
    async def go() -> None:
        session, peer = await hand_made()
        stuck = asyncio.ensure_future(search(session, "x" * (4 << 20), deadline=0.5))
        await asyncio.sleep(0.2)
        try:
            await asyncio.wait_for(session.close(), 5)  # 2 s for `shutdown`, then the kill
            assert session.state != "ready"
        finally:
            stuck.cancel()
            await _settle(session, peer)

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_review_rpc_error_with_a_non_integer_code_marks_the_plugin_in_error() -> None:
    """`{"error": {"code": "boom"}}`: `int("boom")` raises inside the read loop, which dies
    unseen. The call waits out its whole deadline (30 s, 300 s for a download) and the session
    stays `ready` meanwhile; a protocol violation ends the session at once everywhere else."""

    async def go() -> None:
        session, peer = await hand_made()
        try:
            calling = asyncio.ensure_future(search(session, "q"))
            request = await asyncio.wait_for(peer.read(), 5)
            await peer.send(
                {"jsonrpc": "2.0", "id": request["id"], "error": {"code": "boom", "message": "m"}}
            )
            with pytest.raises(protocol.RpcError):
                await asyncio.wait_for(calling, 3)
            assert session.state == "error"
        finally:
            await _settle(session, peer)

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_review_rpc_response_id_true_is_not_request_one() -> None:
    """JSON `true` is not the id 1: `isinstance(True, int)` lets a boolean answer request 1."""

    async def go() -> None:
        channel, peer = await pair()

        async def play() -> None:
            hello = await peer.read()
            assert hello["id"] == 1
            await peer.send({"jsonrpc": "2.0", "id": True, "result": {"rpc_protocol": 1}})
            manifest = await peer.read()
            await peer.reply(manifest, MANIFEST)

        playing = asyncio.ensure_future(play())
        try:
            with pytest.raises(protocol.RpcError):
                session = await asyncio.wait_for(
                    RpcSession.open(channel, key="rpc-demo", limits=RpcLimits(call_deadline=2)), 5
                )
                await _settle(session)
        finally:
            playing.cancel()
            peer.writer.close()

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_review_rpc_plugin_cannot_publish_during_the_handshake() -> None:
    """`hello`, `manifest` and `shutdown` are registered as host calls (`_calls`), so a plugin can
    name their id and publish an event before its manifest is read or admitted, and while the
    host is stopping it. Only a call of the contract opens the plugin's way to the host."""
    events: list[Any] = []

    async def go() -> None:
        channel, peer = await pair()

        async def play() -> None:
            hello = await peer.read()
            await peer.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "events.publish",
                    "params": {"call": hello["id"], "name": "early", "payload": {}},
                }
            )
            await peer.read()  # the host's answer to that request
            await peer.reply(hello, {"rpc_protocol": 1})
            while (message := await peer.read()).get("method") != "manifest":
                pass
            await peer.reply(message, MANIFEST)

        def sink(key: str, name: str, payload: Any) -> None:
            events.append((key, name, payload))

        playing = asyncio.ensure_future(play())
        session = None
        try:
            with contextlib.suppress(protocol.RpcError, TimeoutError):
                session = await asyncio.wait_for(
                    RpcSession.open(
                        channel, key="rpc-demo", limits=RpcLimits(call_deadline=2), on_event=sink
                    ),
                    5,
                )
            await asyncio.sleep(0.1)
        finally:
            playing.cancel()
            if session is not None:
                await _settle(session, peer)
            peer.writer.close()

    run(go)
    assert events == []


@pytest.mark.req("EF-SRC-016")
def test_review_rpc_close_kills_what_the_plugin_left_running(tmp_path: Path) -> None:
    """The plugin starts a child in its own process group, then exits. `exit_reason()` reaps the
    plugin, so `close()` sees a return code and skips `killpg`: the child runs on, unowned."""
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
    )
    limits = RpcLimits(call_deadline=5)
    child: int | None = None

    async def go() -> None:
        channel = await spawn_stdio([sys.executable, "-I", "-c", code], limits=limits)
        with pytest.raises(protocol.RpcError):
            await RpcSession.open(channel, key="rpc-demo", limits=limits)

    try:
        run(go)
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _alive(child):
            time.sleep(0.05)
        assert not _alive(child), "the plugin's child outlived the session"
    finally:
        if child is None and pidfile.exists():
            child = int(pidfile.read_text())
        if child is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(child, signal.SIGKILL)


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


@pytest.mark.req("EF-SRC-016")
def test_review_rpc_failure_reason_is_bounded_and_does_not_stall_the_host() -> None:
    """The reason a plugin is marked in error with is partly the plugin's text: a duplicate key
    is quoted whole (`FrameError(f"duplicate key {key!r}")`), then logged through `_scrubbed`,
    i.e. `redact_value`, on the host's event loop, and kept whole by `mark_error`. The log
    redaction is quadratic on a long run of letters without a digit (`_TOKEN_SEGMENT`'s
    lookaheads): 4 KiB take 1 s, 16 KiB 16 s, 1 MiB hours, during which no other plugin, request
    or job of the host runs. One frame is enough."""

    async def go() -> None:
        session, peer = await hand_made()
        try:
            key = "k" * (16 << 10)
            payload = f'{{"jsonrpc":"2.0","{key}":1,"{key}":1}}'.encode()
            started = time.monotonic()
            peer.writer.write(struct.pack(">I", len(payload)) + payload)
            await peer.writer.drain()
            while session.state == "ready" and time.monotonic() - started < 30:
                await asyncio.sleep(0.01)
            stalled = time.monotonic() - started
            assert session.state == "error"
            assert stalled < 2, f"the host's event loop was held {stalled:.1f}s by one frame"
            assert len(session.reason or "") <= 2000
        finally:
            await _settle(session, peer)

    run(go)


SECRET = "tok-" + "5e1d9a2c7b3f8046"  # generated for the test, never a real key


class EchoFetcher:
    """A host Fetcher whose server echoes the credential the host injected."""

    def __init__(self, *, header: bool) -> None:
        self.header = header

    async def get(self, url: str, **kw: Any) -> Response:
        if self.header:
            return Response(status=200, headers={"X-Echo": SECRET}, body=b"")
        return Response(status=200, body=("x" + SECRET).encode())

    async def post(self, url: str, **kw: Any) -> Response:
        return Response(status=200)

    async def download(self, req: ImageRequest) -> DownloadedFile:
        return DownloadedFile(sha256="ab", size=0, content=b"")

    def settings(self) -> dict[str, Any]:
        return {}

    def cookies(self) -> CookieJarView:
        return CookieJarView()


class Echo(Plugin):
    async def search(self, f: Any, q: str, filters: dict[str, Any], cursor: Any, **kw: Any) -> Any:
        try:
            got = await f.get("https://ok.example.org/")
        except Exception as exc:
            return Page(items=[_ref(f"refused:{getattr(exc, 'kind', type(exc).__name__)}")])
        return Page(items=[_ref(got.body.decode() + "|" + "|".join(got.headers.values()))])


def _ref(title: str) -> SeriesRef:
    return SeriesRef(source_key="rpc-demo", remote_id="1", title=title)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_review_rpc_answer_withheld_for_a_secret_is_still_answered() -> None:
    """`_write` refuses an answer that carries a secret (`SecretLeak`), but `_serve` only catches
    `FrameTooLarge`: the task dies, the plugin waits for an answer that never comes, and the call
    ends on its deadline with the plugin marked in error for a timeout it did not cause."""
    register_secret_value(SECRET)

    async def go() -> None:
        session, served = await connect(Echo())
        try:
            page = await asyncio.wait_for(
                session.call(
                    "source.search",
                    {"q": "x", "filters": {}, "cursor": None},
                    fetcher=EchoFetcher(header=True),
                ),
                5,
            )
            assert page.items[0].title.startswith("refused:")
            assert SECRET not in page.items[0].title
            assert session.state == "ready"
        finally:
            await _settle(session)
            served.cancel()

    run(go)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_review_rpc_secret_check_sees_bytes_as_well() -> None:
    """The same secret, in the body: bytes travel as base64 and the frame is checked as text, so
    only a body that starts with the secret at a multiple of 3 bytes is caught. One byte before
    it and the credential the host injected reaches the plugin."""
    register_secret_value(SECRET)

    async def go() -> None:
        session, served = await connect(Echo())
        try:
            page = await asyncio.wait_for(
                session.call(
                    "source.search",
                    {"q": "x", "filters": {}, "cursor": None},
                    fetcher=EchoFetcher(header=False),
                ),
                5,
            )
            assert SECRET not in page.items[0].title
        finally:
            await _settle(session)
            served.cancel()

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_review_rpc_proxy_has_no_optional_capability_the_plugin_lacks() -> None:
    """The proxy's docstring: `hasattr` tells optional capabilities apart as it does in process.
    But `export_target.update_metadata` (the optional `MetadataUpdater`) is in the table of every
    export target, so every isolated export target looks able to update metadata."""
    from shelfhold.plugin_host.adapters.rpc.proxy import RpcPluginProxy
    from tests.unit.test_plugin_rpc_serve import Target

    assert not hasattr(Target(), "update_metadata")
    proxy = RpcPluginProxy(None, Target.manifest)  # type: ignore[arg-type]
    assert not hasattr(proxy, "update_metadata")
