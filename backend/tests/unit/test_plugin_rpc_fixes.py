"""M6 L04: what the adverse review asked for that its proofs do not cover.

The proofs themselves are in `test_plugin_rpc_review.py`, which is left as delivered.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from shelfhold_sdk.fetcher import CookieJarView, Response
from shelfhold_sdk.models import API_VERSION, PluginKind
from shelfhold_sdk.source import RateLimited

from shelfhold.plugin_host.registry import PluginOrigin, PluginRegistry, PluginStatus
from shelfhold.plugin_host.rpc import host_side, protocol
from tests.unit.test_plugin_rpc import MANIFEST, hand_made, run
from tests.unit.test_plugin_rpc_serve import Plugin, connect


@pytest.mark.req("EF-SRC-016")
def test_rpc_text_from_a_plugin_is_cut_before_it_is_masked() -> None:
    long = "k" * (1 << 20)
    started = time.monotonic()
    out = host_side._scrubbed("refused " + long)
    assert time.monotonic() - started < 1 and len(out) <= host_side.MAX_REASON


@pytest.mark.req("EF-SRC-016")
def test_rpc_rate_limit_delay_named_by_a_plugin_is_bounded() -> None:
    def wire(value: Any) -> RateLimited:
        error = {"code": protocol.PLUGIN_RAISED, "message": "m",
                 "data": {"type": "RateLimited", "retry_after": value}}  # fmt: skip
        raised = protocol.exception_from_wire(error)
        assert isinstance(raised, RateLimited)
        return raised

    assert wire(1e12).retry_after == 3600.0 and wire(-5).retry_after == 0.0
    assert wire(float("nan")).retry_after is None and wire(True).retry_after is None
    assert wire(12.5).retry_after == 12.5


@pytest.mark.req("EF-SRC-016")
def test_rpc_requests_a_plugin_left_running_end_with_the_call() -> None:
    started = asyncio.Event()
    cancelled: list[bool] = []

    class Blocking:
        async def get(self, url: str, **kw: Any) -> Response:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return Response(status=200)

        def settings(self) -> dict[str, Any]:
            return {}

        def cookies(self) -> CookieJarView:
            return CookieJarView()

    class Leaver(Plugin):
        async def search(self, f: Any, q: str, filters: Any, cursor: Any, **kw: Any) -> Any:
            self.kept = asyncio.ensure_future(f.get("https://ok.example.org/"))  # never awaited
            await asyncio.sleep(0.2)
            return await super().search(f, "done", filters, cursor)

    async def go() -> None:
        session, served = await connect(Leaver())
        try:
            await session.call(
                "source.search", {"q": "x", "filters": {}, "cursor": None}, fetcher=Blocking()
            )
            await asyncio.sleep(0.1)
            assert started.is_set() and cancelled == [True]
        finally:
            await session.close()
            served.cancel()

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_rpc_tracker_plugin_is_refused_until_its_contract_exists() -> None:
    async def go() -> None:
        session, _peer = await hand_made()
        session.manifest = {**MANIFEST, "kind": PluginKind.TRACKER.value, "api": API_VERSION}
        manifest = session.manifest
        manifest.pop("langs"), manifest.pop("content_kinds")

        async def connect_it() -> Any:
            return session

        record = await PluginRegistry().load_isolated(
            "rpc-demo", PluginOrigin.THIRD_PARTY, connect_it
        )
        assert record.status is PluginStatus.INCOMPATIBLE and "tracker" in (record.reason or "")

    run(go)


@pytest.mark.req("EF-SRC-016")
def test_rpc_update_metadata_is_there_only_when_the_target_declares_it() -> None:
    assert (
        protocol.host_methods(PluginKind.EXPORT_TARGET)["export_target.config_schema"] is not None
    )
    assert "export_target.update_metadata" not in protocol.host_methods(PluginKind.EXPORT_TARGET)
    declared = protocol.host_methods(PluginKind.EXPORT_TARGET, frozenset({"update_metadata"}))
    assert "export_target.update_metadata" in declared
