"""Simulated Suwayomi over HTTP for the E2E (EF-SRC-008): the in-process fake of the backend
tests behind a socket. Basic authentication, the synthetic library of
`backend/tests/fixtures/suwayomi_states/library.json`, the CBZ of `fixtures/synthetic/`.

POST /_fake/publish releases the second chapter of the first series upstream;
POST /_fake/twin gives the second series the title of the first (another language)
and a second chapter, published with the first one's; POST /_fake/reinstall renumbers every local id (the tool reinstalled, EF-SRC-036); GET /_fake/state
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
twin_chapters: list[dict[str, object]] = []  # filled by /_fake/twin, published with the rest


def handle(method: str, target: str, headers: dict[str, str], body: bytes):  # type: ignore[no-untyped-def]
    path = target.split("?", 1)[0]
    if path == "/_fake/reinstall" and method == "POST":
        fake.reinstall()
        return 200, {}, b"{}"
    if path == "/_fake/publish" and method == "POST":
        _alpha.extend(unpublished)
        unpublished.clear()
        fake.state["mangas"][1]["chapters"].extend(twin_chapters)
        twin_chapters.clear()
        return 200, {}, b"{}"
    if path == "/_fake/twin" and method == "POST":
        # Two series with one title in two languages (en, fr): the same series name twice.
        beta = fake.state["mangas"][1]
        beta["title"] = fake.state["mangas"][0]["title"]
        first = beta["chapters"][0]
        twin_chapters.append(
            {**first, "id": 5010, "url": "/series-beta/ch-002", "name": "Chapter 2",
             "chapterNumber": 2.0, "sourceOrder": 1}  # fmt: skip
        )
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
