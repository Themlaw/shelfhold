"""A small source plugin for the M6 crash journey. Only the SDK and the standard library: it is
built into a wheel by the test (once per key, `KEY` is replaced), installed on the plugins volume
and run in the companion's subprocess.

`search` answers with one reference whose title is `echo:<query>`; asked for `crash`, the plugin's
own process exits at once (status 3), with no answer and no goodbye, as a plugin that breaks does.
"""

from __future__ import annotations

import os
from typing import Any

from shelfhold_sdk.models import API_VERSION, Cursor, Page, PluginKind, RateLimit
from shelfhold_sdk.refs import SeriesRef
from shelfhold_sdk.source import Capability, SourceManifest

KEY = "__KEY__"


class Plugin:
    manifest = SourceManifest(
        key=KEY,
        kind=PluginKind.SOURCE,
        version="1.0.0",
        api=API_VERSION,
        name=f"Plugin {KEY}",
        langs=["en"],
        content_kinds={"text"},
        url_patterns=[],
        capabilities=Capability.SEARCH,
        rate_limit=RateLimit(requests_per_second=1.0),
    )

    def filters(self) -> list[Any]:
        return []

    async def search(
        self,
        f: Any,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        if q == "crash":
            os._exit(3)
        ref = SeriesRef(source_key=KEY, remote_id="1", title=f"echo:{q}")
        return Page(items=[ref], next=None)


def make() -> Plugin:
    return Plugin()
