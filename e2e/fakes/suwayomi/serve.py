"""Simulated Suwayomi over HTTP for the E2E (EF-SRC-008): the in-process fake of the backend
tests behind a socket. Basic authentication, the synthetic library of
`backend/tests/fixtures/suwayomi_states/library.json`, the CBZ of `fixtures/synthetic/`.

POST /_fake/publish releases the second chapter of the first series upstream;
POST /_fake/reinstall renumbers every local id (the tool reinstalled, EF-SRC-036); GET /_fake/state
tells what was asked of it. Never used by Shelfhold.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from tests.fakes.suwayomi import FakeSuwayomi  # type: ignore[import-not-found]

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from httpshell import serve  # noqa: E402

fake = FakeSuwayomi(auth_mode=os.environ.get("FAKE_SUWAYOMI_AUTH", "basic_auth"))
# The series starts with its first chapter only; the test publishes the next one upstream.
_alpha = fake.state["mangas"][0]["chapters"]
unpublished = [_alpha.pop()] if len(_alpha) > 1 else []


def handle(method: str, target: str, headers: dict[str, str], body: bytes):  # type: ignore[no-untyped-def]
    path = target.split("?", 1)[0]
    if path == "/_fake/reinstall" and method == "POST":
        fake.reinstall()
        return 200, {}, b"{}"
    if path == "/_fake/publish" and method == "POST":
        _alpha.extend(unpublished)
        unpublished.clear()
        return 200, {}, b"{}"
    if path == "/_fake/state":
        state = {"requests": [f"{r.method} {r.path}" for r in fake.requests],
                 "violations": fake.violations,
                 "categories": fake.categories}  # fmt: skip
        return 200, {"content-type": "application/json"}, json.dumps(state).encode()
    response = fake.handle(method, target, headers, body)
    return response.status, dict(response.headers), response.body


if __name__ == "__main__":
    serve("127.0.0.1", int(os.environ.get("PORT", "4567")), handle, "fake-suwayomi")
