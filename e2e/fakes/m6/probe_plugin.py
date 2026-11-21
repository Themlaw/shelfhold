"""A source plugin that tells what its own process sees (M6 L05). Only the SDK and the standard
library: it is installed on the volume the companion reads and runs in the companion's subprocess.
`search` answers with one reference whose title is a JSON document: the uid, the four paths the
isolation check looks for (when they are visible), the names of its environment, the interfaces
and what the kit's isolation check says of it."""

from __future__ import annotations

import json
import os
import socket
from typing import Any

from shelfhold_sdk.models import API_VERSION, Cursor, Page, PluginKind, RateLimit
from shelfhold_sdk.refs import SeriesRef
from shelfhold_sdk.source import Capability, SourceManifest
from shelfhold_sdk.testing.isolation import observe_sandbox, run_isolation_check


def _outbound() -> bool:
    probe = socket.socket()
    probe.settimeout(1.0)
    try:
        probe.connect(("192.0.2.1", 80))
    except OSError:
        return False
    finally:
        probe.close()
    return True


class Plugin:
    manifest = SourceManifest(
        key="e2e-probe",
        kind=PluginKind.SOURCE,
        version="1.0.0",
        api=API_VERSION,
        name="Probe",
        langs=["en"],
        content_kinds={"text"},
        url_patterns=[],
        capabilities=Capability.SEARCH,
        rate_limit=RateLimit(requests_per_second=1.0),
    )

    def filters(self) -> list[Any]:
        return []

    async def search(
        self, f: Any, q: str, filters: dict[str, Any], cursor: Cursor | None, *, sub: str | None = None
    ) -> Page[SeriesRef]:
        core_uid = int(filters.get("core_uid", -1))
        view = observe_sandbox()
        report = run_isolation_check(view, core_uid=core_uid)
        seen = {
            "uid": os.getuid(),
            "visible": list(view.visible),
            "env_names": sorted(os.environ),
            "interfaces": sorted(os.listdir("/sys/class/net")),
            "outbound": _outbound(),
            "isolation": {c.name: c.ok for c in report.checks},
        }
        return Page(
            items=[SeriesRef(source_key="e2e-probe", remote_id="1", title=json.dumps(seen))],
            next=cursor,
        )


def make() -> Plugin:
    return Plugin()
