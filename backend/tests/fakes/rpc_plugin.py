"""A source plugin for the isolated-plugin tests: it runs in a real subprocess, behind the RPC.

What it does depends on the query of `search`, so that one plugin can crash, hang, spin, take too
much memory or call the host back. Only the SDK and the standard library are imported.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from shelfhold_sdk.models import API_VERSION, Cursor, Page, PluginKind, RateLimit
from shelfhold_sdk.refs import SeriesRef
from shelfhold_sdk.source import Capability, SourceManifest

_RUNNING = 0
_PEAK = 0


def manifest(**over: Any) -> SourceManifest:
    base: dict[str, Any] = {
        "key": "rpc-demo",
        "kind": PluginKind.SOURCE,
        "version": "1.0.0",
        "api": API_VERSION,
        "name": "RPC demo",
        "langs": ["en"],
        "content_kinds": {"text"},
        "url_patterns": [],
        "capabilities": Capability.SEARCH,
        "rate_limit": RateLimit(requests_per_second=1.0),
    }
    return SourceManifest(**{**base, **over})


class Plugin:
    manifest = manifest()

    def filters(self) -> list[Any]:
        return []

    def migrate_ref(self, old_source_key: str, old_remote_id: str) -> tuple[str, str] | None:
        return (old_source_key, old_remote_id.lower())

    async def search(
        self,
        f: Any,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        global _RUNNING, _PEAK
        if q == "crash":
            os._exit(3)
        if q == "sleep":
            await asyncio.sleep(3600)
        if q == "spin":
            while True:
                pass
        if q == "memory":
            return Page(items=[_ref(str(len(bytearray(900 << 20))))])
        if q == "huge":
            return Page(items=[_ref("x" * (9 << 20))])
        if q == "fetch":
            got = await f.get(filters["url"])
            return Page(items=[_ref(f"{got.status}:{got.body.decode()}:{sorted(got.headers)}")])
        if q == "fetch-error":
            try:
                await f.get(filters["url"])
            except Exception as exc:  # the host's refusal, as the plugin sees it
                return Page(items=[_ref(f"{getattr(exc, 'kind', type(exc).__name__)}|{exc}")])
        if q == "env":
            return Page(items=[_ref("|".join(sorted(os.environ)))])
        if q == "peak":
            _RUNNING += 1
            _PEAK = max(_PEAK, _RUNNING)
            await asyncio.sleep(0.15)
            _RUNNING -= 1
            return Page(items=[_ref(str(_PEAK))])
        if q == "live":
            return Page(items=[_ref(lambda: None)])  # type: ignore[arg-type]
        return Page(items=[_ref(f"echo:{q}")], next=cursor)


def _ref(title: str) -> SeriesRef:
    return SeriesRef(source_key="rpc-demo", remote_id="1", title=title)


def make() -> Plugin:
    return Plugin()


def make_other() -> Plugin:
    plugin = Plugin()
    plugin.manifest = manifest(key="rpc-demo-2")
    return plugin
