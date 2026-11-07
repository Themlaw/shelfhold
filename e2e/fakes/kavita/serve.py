"""Simulated Kavita over HTTP for the E2E (EF-INT-024): the in-process fake of the backend tests
behind a socket, plus what an in-process fake does not need.

- the two set-up routes of `shelfhold target add kavita` (login, named key), which the operating
  client's allow-list refuses and the fake therefore does not know;
- a scan that really looks at the shared library volume: a `scan-folder` call makes Kavita index
  the CBZ that Shelfhold wrote there, so "readable in Kavita" is proved by the files on disk.

GET /_fake/state is the test's window on it (never used by Shelfhold).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx2

from tests.fakes.kavita import API_KEY, FakeKavita  # type: ignore[import-not-found]

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from httpshell import serve  # noqa: E402

LIBRARY_ROOT = Path(os.environ.get("FAKE_LIBRARY_ROOT", "/library"))
ADMIN = os.environ.get("FAKE_KAVITA_USER", "shelfhold")
PASSWORD = os.environ.get("FAKE_KAVITA_PASSWORD_VALUE", "synthetic-kavita-password")
TOKEN = "synthetic-session-token"  # noqa: S105

fake = FakeKavita()
fake.libraries = []  # a fresh installation: `shelf create` makes the library
keys: list[dict[str, object]] = []


def _json(payload: object, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    return status, {"content-type": "application/json"}, json.dumps(payload).encode()


def _scan(body: bytes) -> None:
    """Index what is on disk under the scanned folder, one series per folder holding CBZ."""
    folder = json.loads(body).get("folderPath") or ""
    root = Path(folder)
    if not root.is_dir():
        return
    library = next(
        (
            lib
            for lib in fake.libraries
            if any(folder == f or folder.startswith(f.rstrip("/") + "/") for f in lib["folders"])
        ),
        None,
    )
    if library is None:
        return
    for directory in sorted({p.parent for p in root.rglob("*.cb[zr]")}):
        if any(part.startswith(".") for part in directory.relative_to(root).parts):
            continue  # Kavita is told to ignore the reserved area; the fake agrees
        files = sorted(str(p) for p in directory.glob("*.cb[zr]"))
        fake.discover(str(directory), files, library_id=library["id"])


def handle(method: str, target: str, headers: dict[str, str], body: bytes):  # type: ignore[no-untyped-def]
    path = target.split("?", 1)[0]
    if path == "/_fake/state":
        return _json({"series": fake.series, "files": {str(k): v for k, v in fake.files.items()},
                      "libraries": fake.libraries, "violations": fake.violations,
                      "scans": fake.scans, "keys": keys})  # fmt: skip
    lowered = path.lower()
    if path == "/_fake/foreign_series" and method == "POST":
        # a series Kavita holds from another source, in the first library: ?name=<name>
        from urllib.parse import parse_qs, urlsplit

        name = parse_qs(urlsplit(target).query)["name"][0]
        library = fake.libraries[0]
        folder = f"{library['folders'][0].rstrip('/')}/elsewhere/{name}"
        fake.add_series(name, folder, [f"{folder}/1.cbz"], library_id=library["id"])
        return _json({})
    if (method, lowered) == ("POST", "/api/account/login"):
        data = json.loads(body or b"{}")
        if data.get("username") != ADMIN or data.get("password") != PASSWORD:
            return 401, {}, b"Unauthorized"
        return _json({"username": ADMIN, "token": TOKEN, "apiKey": "default-key-never-read"})
    if lowered == "/api/account/create-auth-key" and method == "POST":
        if headers.get("authorization") != f"Bearer {TOKEN}":
            return 401, {}, b"Unauthorized"
        asked = json.loads(body or b"{}")
        entry = {"id": len(keys) + 1, "name": asked.get("name"), "key": API_KEY,
                 "expiresAtUtc": asked.get("expiresUtc")}  # fmt: skip
        keys.append(entry)
        return _json(entry)
    if lowered == "/api/account/auth-keys" and method == "GET":
        return _json(keys)
    if lowered == "/api/account/auth-key" and method == "DELETE":
        keys.clear()
        return 200, {}, b""
    if lowered == "/api/library/scan-folder" and headers.get("x-api-key") == API_KEY:
        _scan(body)
    request = httpx2.Request(method, f"http://kavita.test{target}", headers=headers, content=body)
    response = fake.handle(request)
    return response.status_code, {k: v for k, v in response.headers.items()
                                  if k.lower() == "content-type"}, response.content  # fmt: skip


if __name__ == "__main__":
    serve("127.0.0.1", int(os.environ.get("PORT", "5000")), handle, "fake-kavita")
