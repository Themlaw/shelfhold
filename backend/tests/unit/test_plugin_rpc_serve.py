"""M6 L04: both ends of `rpc_protocol` 1 in one process, to cover what a subprocess cannot show.

The plugin side runs `plugin_side.serve` on one end of a socket pair and the host's `RpcSession` on
the other: the contract methods of the three kinds, errors across the wire, the Fetcher and events
served to the plugin, and plans that come back validated.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from shelfhold_sdk.api_call import ApiCall
from shelfhold_sdk.fetcher import CookieJarView, DownloadedFile, Response
from shelfhold_sdk.models import API_VERSION, Cursor, Page, PluginKind, PluginManifest
from shelfhold_sdk.refs import ImageRequest, SeriesRef
from shelfhold_sdk.source import AuthRequired, RateLimited

from shelfhold.plugin_host.adapters.rpc.proxy import RpcPluginProxy
from shelfhold.plugin_host.registry import PluginOrigin, PluginRegistry, PluginStatus
from shelfhold.plugin_host.rpc import plugin_side, protocol
from shelfhold.plugin_host.rpc.host_side import RpcSession
from shelfhold.plugin_host.rpc.protocol import Channel, RpcLimits
from tests.fakes.rpc_plugin import manifest as source_manifest

NEXT = "n"


def run[T](make: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(asyncio.wait_for(make(), 30))


class Plugin:
    manifest = source_manifest()

    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def search(
        self,
        f: Any,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        if q == "auth":
            raise AuthRequired("log in")
        if q == "rate":
            raise RateLimited("slow down", 12.5)
        if q == "boom":
            raise RuntimeError("secret internals")
        if q == "post":
            got = await f.post("https://ok.example.org/x", data={"a": "1"}, json={"b": 2})
            return Page(items=[_ref(f"{got.status}")])
        if q == "download":
            got = await f.download(ImageRequest(url="https://ok.example.org/f"))
            return Page(items=[_ref(f"{got.size}:{got.sha256}")])
        if q == "context":
            return Page(items=[_ref(f"{f.settings()}|{f.cookies().cookies}|{sub}")])
        if q == "event":
            await plugin_side.publish_event("done", {"n": 1})
            return Page(items=[_ref("published")])
        if q == "late":
            asyncio.get_running_loop().call_later(0, lambda: None)
            return Page(items=[_ref("x")])
        return Page(items=[_ref(q)], next=cursor)

    def migrate_ref(self, old_source_key: str, old_remote_id: str) -> tuple[str, str] | None:
        return None

    def migrate_memo(self, version: int, memo: dict[str, Any]) -> dict[str, Any]:
        return {"v": version, **memo}


def _ref(title: str) -> SeriesRef:
    return SeriesRef(source_key="rpc-demo", remote_id="1", title=title)


class Target:
    manifest = PluginManifest(
        key="rpc-target", kind=PluginKind.EXPORT_TARGET, version="1.0.0", api=API_VERSION, name="t"
    )
    evil = False

    def scan_calls(self, remote_paths: list[str]) -> list[Any]:
        if self.evil:
            return [{"method": "EVIL", "path": "/x", "auth": "none"}]
        body = {"paths": remote_paths}
        return [ApiCall(method="POST", path="/scan", json_body=body, auth="none")]

    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}


class HostFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def get(self, url: str, **kw: Any) -> Response:
        self.calls.append(("get", url))
        return Response(status=200, headers={"Authorization": "x", "X-A": "1"}, body=b"b")

    async def post(self, url: str, **kw: Any) -> Response:
        self.calls.append(("post", kw))
        return Response(status=201)

    async def download(self, req: ImageRequest) -> DownloadedFile:
        return DownloadedFile(sha256="ab", size=3, content=b"\x00\x01\x02")

    def settings(self) -> dict[str, Any]:
        return {"language": "en"}

    def cookies(self) -> CookieJarView:
        return CookieJarView(cookies={"c": "1"})


async def connect(
    instance: Any, events: list[Any] | None = None, limits: RpcLimits | None = None
) -> tuple[RpcSession, asyncio.Task[None]]:
    left, right = socket.socketpair()
    host_r, host_w = await asyncio.open_connection(sock=left)
    plug_r, plug_w = await asyncio.open_connection(sock=right)
    served = asyncio.ensure_future(plugin_side.serve(instance, plug_r, plug_w))

    async def close() -> None:
        host_w.close()

    async def exit_reason() -> str | None:
        return None

    def on_event(key: str, name: str, payload: Any) -> None:
        if events is not None:
            events.append((key, name, payload))

    session = await RpcSession.open(
        Channel(host_r, host_w, close, exit_reason),
        key=instance.manifest.key,
        limits=limits or RpcLimits(),
        on_event=on_event if events is not None else None,
    )
    return session, served


@pytest.mark.req("EF-SRC-016")
def test_rpc_served_methods_errors_and_fetcher_calls() -> None:
    async def go() -> None:
        session, served = await connect(Plugin(), events=[])
        proxy = RpcPluginProxy(session, Plugin.manifest)
        fetcher = HostFetcher()
        try:
            page = await proxy.search(fetcher, "hello", {}, Cursor(token=NEXT))
            assert page.items[0].title == "hello" and page.next == Cursor(token=NEXT)
            with pytest.raises(AuthRequired):  # SDK errors come back as themselves
                await proxy.search(fetcher, "auth", {}, None)
            with pytest.raises(RateLimited) as limited:
                await proxy.search(fetcher, "rate", {}, None)
            assert limited.value.retry_after == 12.5
            with pytest.raises(protocol.RemoteError) as boom:  # the rest: named, not rebuilt
                await proxy.search(fetcher, "boom", {}, None)
            assert boom.value.code == protocol.PLUGIN_RAISED and session.state == "ready"
            assert (await proxy.search(fetcher, "post", {}, None)).items[0].title == "201"
            assert fetcher.calls[-1][1]["json"] == {"b": 2}
            done = await proxy.search(fetcher, "download", {}, None)
            assert done.items[0].title == "3:ab"
            ctx = await proxy.search(fetcher, "context", {}, None, sub="s1")
            assert ctx.items[0].title == "{'language': 'en'}|{'c': '1'}|s1"
            assert (await proxy.search(fetcher, "event", {}, None)).items[0].title == "published"
            assert await proxy.migrate_ref("a", "B") is None
            assert not hasattr(proxy, "scan_calls") and not hasattr(proxy, "_private")
            with pytest.raises(AttributeError):
                proxy.nothing  # noqa: B018
        finally:
            await session.close()
            await asyncio.wait_for(served, 5)

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_rpc_events_are_named_by_the_host_in_the_plugins_own_space() -> None:
    events: list[Any] = []

    async def go() -> None:
        session, served = await connect(Plugin(), events=events)
        try:
            await session.call(
                "source.search",
                {"q": "event", "filters": {}, "cursor": None},
                fetcher=HostFetcher(),
            )
        finally:
            await session.close()
            await asyncio.wait_for(served, 5)

    run(go)
    assert events == [("rpc-demo", "done", {"n": 1})]  # the plugin gave a name; the host names it

    async def refused() -> None:
        session, served = await connect(Plugin(), events=None)  # no sink wired: refused, not lost
        try:
            with pytest.raises(protocol.RemoteError) as err:
                await session.call(
                    "source.search", {"q": "event", "filters": {}, "cursor": None}, fetcher=None
                )
            assert "HostRefused" in str(err.value.data)
        finally:
            await session.close()
            await asyncio.wait_for(served, 5)

    run(refused)


@pytest.mark.req("EF-SRC-016")
def test_rpc_response_headers_that_carry_a_session_are_withheld() -> None:
    async def go() -> None:
        session, served = await connect(Plugin())
        try:
            wired = await session._request(
                "source.search",
                {"args": {"q": "x", "filters": {}, "cursor": None}, "context": {}},
                5,
                fetcher=HostFetcher(),
            )
            assert wired["items"][0]["title"] == "x"
        finally:
            await session.close()
            await asyncio.wait_for(served, 5)

    run(go)

    # what the host would hand back for `fetcher.get`: no Authorization, no Set-Cookie
    class Probe(Plugin):
        async def search(
            self, f: Any, q: str, filters: dict[str, Any], cursor: Any, **kw: Any
        ) -> Any:
            got = await f.get("https://ok.example.org/")
            return Page(items=[_ref(",".join(sorted(got.headers)))])

    async def headers() -> None:
        session, served = await connect(Probe())
        try:
            page = await session.call(
                "source.search", {"q": "x", "filters": {}, "cursor": None}, fetcher=HostFetcher()
            )
            assert page.items[0].title.lower() == "x-a"
        finally:
            await session.close()
            await asyncio.wait_for(served, 5)

    run(headers)


@pytest.mark.req("EF-SRC-016")
def test_rpc_plans_come_back_validated_and_a_bad_plan_is_refused() -> None:
    async def go() -> None:
        target = Target()
        session, served = await connect(target)
        try:
            wire = "export_target.scan_calls"
            calls = await session.call(wire, {"remote_paths": ["/a"]}, fetcher=None)
            # a plan, never an object
            assert isinstance(calls[0], ApiCall) and calls[0].path == "/scan"
            assert await session.call("export_target.config_schema", {}, fetcher=None) == {
                "type": "object"
            }
            target.evil = True  # a hostile plugin describing a call the model does not know
            with pytest.raises(protocol.RemoteError) as bad:
                await session.call("export_target.scan_calls", {"remote_paths": []}, fetcher=None)
            assert bad.value.code == protocol.INVALID_RESULT and session.state == "ready"
        finally:
            await session.close()
            await asyncio.wait_for(served, 5)

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_rpc_plugin_side_refuses_bad_requests_and_keeps_serving() -> None:
    async def go() -> None:
        session, served = await connect(Plugin())
        try:
            wrong: list[dict[str, Any]] = [
                {"q": "x", "filters": {}},
                {"q": "x", "filters": {}, "cursor": 5},
                {"q": "x", "filters": {}, "cursor": None, "extra": 1},
            ]
            for args in wrong:
                with pytest.raises(protocol.RemoteError) as bad:
                    await session.call("source.search", args, fetcher=None)
                assert bad.value.code == protocol.INVALID_PARAMS
            # a plugin that has been told nothing of a method answers "not found", the call fails
            ok = await session.call(
                "source.search", {"q": "fine", "filters": {}, "cursor": None}, fetcher=None
            )
            assert ok.items[0].title == "fine"
            with pytest.raises(plugin_side.HostRefused):  # the plugin asks outside any host call
                await plugin_side.publish_event("late")
        finally:
            await session.close()
            await asyncio.wait_for(served, 5)

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_rpc_plugin_side_refuses_a_request_before_hello_and_a_future_protocol() -> None:
    from shelfhold.plugin_host.rpc import codec

    async def bare(first: dict[str, Any]) -> dict[str, Any] | None:
        left, right = socket.socketpair()
        r, w = await asyncio.open_connection(sock=left)
        pr, pw = await asyncio.open_connection(sock=right)
        served = asyncio.ensure_future(plugin_side.serve(Plugin(), pr, pw))
        await codec.write_frame(w, first)
        payload = await asyncio.wait_for(codec.read_frame(r), 5)
        w.close()
        await asyncio.wait_for(served, 5)
        return None if payload is None else codec.decode_payload(payload)

    # the manifest is never given before `hello`: the connection simply ends
    assert run(lambda: bare({"jsonrpc": "2.0", "id": 1, "method": "manifest"})) is None
    hello = {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"rpc_protocol": [2]}}
    future = run(lambda: bare(hello))
    assert future is not None and future["error"]["code"] == protocol.NO_COMMON_PROTOCOL


@pytest.mark.req("EF-SRC-016")
def test_rpc_registry_marks_a_plugin_in_error_when_its_session_fails() -> None:
    async def go() -> None:
        reg = PluginRegistry()

        async def open_it() -> RpcSession:
            session, _ = await connect(Plugin())
            return session

        record = await reg.load_isolated("rpc-demo", PluginOrigin.THIRD_PARTY, open_it)
        assert record.status is PluginStatus.LOADED
        again = await reg.load_isolated("rpc-demo", PluginOrigin.THIRD_PARTY, open_it)
        assert again.status is PluginStatus.INCOMPATIBLE and "duplicate_key" in (again.reason or "")
        await record.instance.session._fail("simulated")
        held = reg.get("rpc-demo")
        assert held is not None and held.status is PluginStatus.ERROR and held.reason == "simulated"
        assert reg.active() == []

        async def broken() -> RpcSession:
            raise protocol.PluginFailed("exited with code 1")

        failed = await PluginRegistry().load_isolated("x", PluginOrigin.THIRD_PARTY, broken)
        assert failed.status is PluginStatus.ERROR and "start failed" in (failed.reason or "")

    run(go)
