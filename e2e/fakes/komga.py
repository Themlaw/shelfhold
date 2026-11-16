"""Simulated Komga over HTTP for the E2E (M4 L08a, EF-EXP-020): the in-process fake of the backend
tests behind a socket.

A library scan and an `empty-trash` are recorded and answered 202, as Komga does; what Komga
indexes is the folder of the shared library volume, so a test reads the files on disk and
`GET /_fake/state` for what Shelfhold asked (never used by Shelfhold itself).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx2
from tests.fakes.komga import FakeKomga

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httpshell import serve

ROOT = os.environ.get("FAKE_KOMGA_LIBRARY_ROOT", "/library/manga")

fake = FakeKomga()
fake.libraries = [
    {"id": "0SYNTHMANGA01", "name": "Synthetic Manga", "root": ROOT, "unavailable": False}
]


def handle(
    method: str, target: str, headers: dict[str, str], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    if target.split("?", 1)[0] == "/_fake/state" and method == "GET":
        state = {"scans": fake.scans, "emptied": fake.emptied, "violations": fake.violations}
        return 200, {"content-type": "application/json"}, json.dumps(state).encode()
    request = httpx2.Request(method, f"http://komga.test{target}", headers=headers, content=body)
    response = fake.handle(request)
    kept = {k: v for k, v in response.headers.items() if k.lower() == "content-type"}
    return response.status_code, kept, response.content


if __name__ == "__main__":
    serve("127.0.0.1", int(os.environ.get("PORT", "25600")), handle, "fake-komga")
