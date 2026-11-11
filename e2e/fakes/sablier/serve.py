"""Simulated Sablier over HTTP for the E2E (EF-INT-022): the blocking strategy and nothing else.

`GET /api/strategies/blocking?names=<n>` wakes the simulated tool it fronts (POST /_fake/wake
on it, same network namespace) and answers once it is ready, in the form of the real API.
GET /_fake/state lists the calls received (never used by Shelfhold). Synthetic, no outbound call.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from httpshell import serve  # noqa: E402

TOOLS = {"fake-suwayomi": "http://127.0.0.1:4567", "fake-kavita": "http://127.0.0.1:5000"}
calls: list[dict[str, object]] = []


def _json(payload: object, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    return status, {"content-type": "application/json"}, json.dumps(payload).encode()


def handle(method: str, target: str, headers: dict[str, str], body: bytes):  # type: ignore[no-untyped-def]
    split = urlsplit(target)
    if split.path == "/_fake/state":
        return _json({"calls": calls})
    if split.path != "/api/strategies/blocking" or method != "GET":
        return 404, {}, b"unknown strategy"
    query = parse_qs(split.query)
    names = query.get("names", [])
    calls.append({"names": names, "session_duration": query.get("session_duration", [""])[0]})
    for name in names:
        base = TOOLS.get(name)
        if base:
            urllib.request.urlopen(  # noqa: S310 - the simulated tool, in this namespace
                urllib.request.Request(base + "/_fake/wake", data=b"{}", method="POST"), timeout=5
            ).read()
    instances = [{"instance": {"name": n, "status": "ready"}, "error": None} for n in names]
    return _json({"session": {"instances": instances, "status": "ready"}})


if __name__ == "__main__":
    serve("127.0.0.1", int(os.environ.get("PORT", "10000")), handle, "fake-sablier")
