"""The administrator's own instance of the chapter crawler, simulated: the in-process fake of the
backend tests behind a socket (nothing here is a copy of a third-party server). The window of the
test is `GET /_fake/state` (violations and requests); the instance is the LAN address of the
stack, like Kavita and Suwayomi."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "m2"))
from books import volume  # noqa: E402
from httpshell import serve  # noqa: E402

from tests.fakes.lncrawl import FakeLncrawl  # noqa: E402


class Instance(FakeLncrawl):
    def build_epub(self, count: int) -> bytes:  # the test helper needs pytest; this does not
        return volume(count)


fake = Instance()


def handle(method: str, target: str, headers: dict[str, str], body: bytes):  # type: ignore[no-untyped-def]
    if target.split("?", 1)[0] == "/_fake/state":
        return 200, {"content-type": "application/json"}, json.dumps({
            "violations": fake.violations,
            "requests": [f"{r.method} {r.path}" for r in fake.requests],
            "jobs": list(fake.jobs),
        }).encode()  # fmt: skip
    reply = fake.handle(method, target, headers, body)
    return reply.status, dict(reply.headers or {}), reply.body or b""


if __name__ == "__main__":
    serve("127.0.0.1", int(os.environ.get("PORT", "8090")), handle, "fake-lncrawl")
