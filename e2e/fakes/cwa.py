"""Simulated drop-box ingester for the E2E (M4 L09, EF-EXP-022): the in-process fake of the backend
tests behind a socket.

The ingestion folder is the one of the shared library volume. `POST /_fake/ingest` is one look at
it: every whole EPUB is taken and removed, as the real one does. `GET /_fake/state` says what was
taken and which file was seen half-written or misnamed (never used by Shelfhold itself).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from tests.fakes.cwa import FakeIngest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httpshell import serve

FOLDER = Path(os.environ.get("FAKE_CWA_INGEST_FOLDER", "/library/manga/ingest"))

fake = FakeIngest(FOLDER)


def handle(
    method: str, target: str, headers: dict[str, str], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    path = target.split("?", 1)[0]
    kind = {"content-type": "application/json"}
    if path == "/_fake/ingest" and method == "POST":
        taken = [i.name for i in fake.scan()]
        return 200, kind, json.dumps({"taken": taken}).encode()
    if path == "/_fake/state" and method == "GET":
        state = {"taken": [i.name for i in fake.taken], "violations": fake.violations}
        return 200, kind, json.dumps(state).encode()
    return 404, kind, b"{}"


if __name__ == "__main__":
    serve("127.0.0.1", int(os.environ.get("PORT", "25601")), handle, "fake-cwa")
