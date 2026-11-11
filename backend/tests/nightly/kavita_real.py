"""Harness of the nightly against a real Kavita (L40; EF-EXP-009, ENF-COMP-001, ENF-QUAL-004).

The official image runs on an internal-only network (fixtures/kavita/bootstrap/compose.yaml); the
suite reaches it through the container's address. Set-up goes through the API only, then Shelfhold's
own adapters are used where Shelfhold would use them: the key exchange of `target add kavita` and
the `KavitaClient`. Raw requests remain for what Shelfhold never does (create the first account,
read a reading progress). Synthetic files only, no network but that internal one.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import struct
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import zipfile
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.dto import LibraryType, SeriesInfo
from shelfhold.headless.adapters import HttpKavitaKeyExchange
from shelfhold.headless.wiring import integration_fetcher
from shelfhold.infrastructure.clock import SystemClock

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kavita" / "bootstrap"
COMPOSE_FILE = FIXTURES / "compose.yaml"
BOOTSTRAP: dict[str, Any] = json.loads((FIXTURES / "bootstrap.json").read_text("utf-8"))
PORT = 5000


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


class _Events:
    def __init__(self) -> None:
        self.codes: list[str] = []

    def emit(self, code: str, **detail: Any) -> None:
        self.codes.append(code)


def png(width: int = 8, height: int = 8, shade: int = 128) -> bytes:
    """A real, tiny PNG (grey), generated: no image from anywhere."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + bytes([shade]) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def comicinfo(series: str, number: str, volume: str | None = None, title: str | None = None) -> str:
    parts = [f"<Series>{series}</Series>", f"<Number>{number}</Number>"]
    if volume is not None:
        parts.append(f"<Volume>{volume}</Volume>")
    if title is not None:
        parts.append(f"<Title>{title}</Title>")
    return '<?xml version="1.0" encoding="utf-8"?>\n<ComicInfo>' + "".join(parts) + "</ComicInfo>\n"


def cbz(info: str | None = None, pages: int = 2) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index in range(pages):
            archive.writestr(f"{index + 1:03d}.png", png(shade=60 + index))
        if info is not None:
            archive.writestr("ComicInfo.xml", info)
    return buffer.getvalue()


@dataclass
class RealKavita:
    image: str
    library_dir: Path
    project: str = field(default_factory=lambda: f"nightly-kavita-{uuid.uuid4().hex[:6]}")
    base_url: str = ""
    token: str = field(default="", repr=False)
    key: str = field(default="", repr=False)
    events: _Events = field(default_factory=_Events)
    client: KavitaClient | None = None

    # -- docker -----------------------------------------------------------------------------
    def _compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "KAVITA_IMAGE": self.image, "KAVITA_LIBRARY": str(self.library_dir)}
        return subprocess.run(  # noqa: S603
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", self.project, *args],  # noqa: S607
            env=env, check=True, capture_output=True, text=True, timeout=600,
        )  # fmt: skip

    def _address(self) -> str:
        cid = self._compose("ps", "-q", "kavita").stdout.strip()
        fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
        out = subprocess.run(  # noqa: S603
            ["docker", "inspect", "-f", fmt, cid],  # noqa: S607
            check=True, capture_output=True, text=True,
        )  # fmt: skip
        return out.stdout.strip()

    def start(self) -> None:
        self._compose("up", "-d", "--pull", "missing")
        self.base_url = f"http://{self._address()}:{PORT}"
        deadline = time.monotonic() + float(BOOTSTRAP["ready_timeout_s"])
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"{self.base_url}/api/health", timeout=3)  # noqa: S310
                break
            except (OSError, urllib.error.URLError):
                time.sleep(2)
        else:
            raise TimeoutError("Kavita did not become ready\n" + self.logs()[-3000:])
        self._register_admin()
        self._exchange_key()

    def stop(self) -> None:
        self._compose("down", "-v", "--remove-orphans", "-t", "5")

    def logs(self) -> str:
        return self._compose("logs", "--no-color", "kavita").stdout

    # -- set-up ------------------------------------------------------------------------------
    def request(
        self, method: str, path: str, *, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        all_headers = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(  # noqa: S310
            self.base_url + path, data=data, headers=all_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def _register_admin(self) -> None:
        admin = BOOTSTRAP["admin"]
        status, user = self.request("POST", "/api/Account/register", body=admin)
        assert status == 200, f"first account refused ({status}): {user}"
        status, user = self.request(
            "POST", "/api/Account/login",
            body={"username": admin["username"], "password": admin["password"]},
        )  # fmt: skip
        assert status == 200 and user["token"], user
        self.token = user["token"]

    def _exchange_key(self) -> None:
        """The very exchange of `shelfhold target add kavita`, against the real server."""
        admin = BOOTSTRAP["admin"]
        exchanged = HttpKavitaKeyExchange(integration_fetcher).exchange(
            url=self.base_url, username=admin["username"], password=admin["password"],
            key_name=BOOTSTRAP["key_name"], expires_days=int(BOOTSTRAP["key_days"]),
        )  # fmt: skip
        self.key = exchanged.value
        self.version = exchanged.version
        self.client = KavitaClient(
            integration_fetcher(self.base_url), self.base_url, AdminKey(self.key),
            events=self.events, clock=SystemClock(),
        )  # fmt: skip

    # -- what the suite does -----------------------------------------------------------------
    def write(self, relative: str, content: bytes) -> Path:
        path = self.library_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def remote(self, relative: str = "") -> str:
        return f"{BOOTSTRAP['library_root']}/{relative}".rstrip("/")

    def create_library(
        self, name: str, folders: list[str], *, patterns: tuple[str, ...] | None = None,
        library_type: LibraryType = LibraryType.MANGA,
    ) -> int:  # fmt: skip
        assert self.client is not None
        args: dict[str, Any] = {} if patterns is None else {"exclude_patterns": patterns}
        created = run(self.client.create_library(name, library_type, folders, **args))
        return int(created["id"])

    def scan(self, folder: str) -> None:
        assert self.client is not None
        run(self.client.scan_folder(folder))

    def series(self, library_id: int) -> list[SeriesInfo]:
        assert self.client is not None
        return run(self.client.series_of_library(library_id))

    def wait_series(
        self, library_id: int, ready: Callable[[list[SeriesInfo]], bool], what: str
    ) -> list[SeriesInfo]:
        deadline = time.monotonic() + float(BOOTSTRAP["scan_timeout_s"])
        found: list[SeriesInfo] = []
        while time.monotonic() < deadline:
            found = self.series(library_id)
            if ready(found):
                return found
            time.sleep(3)
        raise AssertionError(f"{what}: not indexed in time; saw {[s.name for s in found]}")

    def settle(self, library_id: int, seconds: float = 25.0) -> list[SeriesInfo]:
        """After a scan that must find nothing: give Kavita its time, then read what it holds."""
        time.sleep(seconds)
        return self.series(library_id)

    def detail(self, series_id: int) -> dict[str, Any]:
        status, body = self.request(
            "GET", f"/api/Series/series-detail?seriesId={series_id}",
            headers={"Authorization": f"Bearer {self.token}"},
        )  # fmt: skip
        assert status == 200, body
        return dict(body)

    def chapters(self, series_id: int) -> list[dict[str, Any]]:
        detail = self.detail(series_id)
        found = list(detail.get("chapters") or []) + list(detail.get("specials") or [])
        for volume in detail.get("volumes") or []:
            found += volume.get("chapters") or []
        return found

    def set_progress(
        self, library_id: int, series_id: int, chapter: dict[str, Any], page: int
    ) -> None:
        status, body = self.request(
            "POST", "/api/Reader/progress",
            body={"libraryId": library_id, "seriesId": series_id, "volumeId": chapter["volumeId"],
                  "chapterId": chapter["id"], "pageNum": page},
            headers={"Authorization": f"Bearer {self.token}"},
        )  # fmt: skip
        assert status == 200, body

    def progress(self, chapter_id: int) -> int:
        status, body = self.request(
            "GET", f"/api/Reader/get-progress?chapterId={chapter_id}",
            headers={"Authorization": f"Bearer {self.token}"},
        )  # fmt: skip
        assert status == 200, body
        return int(body["pageNum"])

    def restricted_account(
        self, username: str, libraries: list[int], *, age_rating: int, include_unknowns: bool
    ) -> str:
        """A member as a child of the household is made: invited by the administrator with an age
        restriction and the "include unknowns" switch, then confirmed. Returns its session token
        (a throwaway password of the job's own)."""
        import urllib.parse

        admin = {"Authorization": f"Bearer {self.token}"}
        status, invite = self.request(
            "POST", "/api/Account/invite",
            body={"email": f"{username}@example.org", "roles": ["Login"], "libraries": libraries,
                  "ageRestriction": {"ageRating": age_rating, "includeUnknowns": include_unknowns}},
            headers=admin,
        )  # fmt: skip
        assert status == 200, invite
        query = urllib.parse.parse_qs(urllib.parse.urlparse(invite["emailLink"]).query)
        status, member = self.request(
            "POST", "/api/Account/confirm-email",
            body={"email": f"{username}@example.org", "username": username,
                  "password": "Synthetic-pass-1!", "token": query["token"][0]},
        )  # fmt: skip
        assert status == 200, member
        status, login = self.request(
            "POST", "/api/Account/login",
            body={"username": username, "password": "Synthetic-pass-1!"},
        )  # fmt: skip
        assert status == 200, login
        return str(login["token"])

    def visible_to(self, token: str) -> set[str]:
        """The series names a session can read, as Kavita itself decides (its own restriction)."""
        status, found = self.request(
            "POST", "/api/Series/all-v2?PageNumber=1&PageSize=100",
            body={"statements": [], "combination": 1, "limitTo": 0,
                  "sortOptions": {"sortField": 1, "isAscending": True}},
            headers={"Authorization": f"Bearer {token}"},
        )  # fmt: skip
        assert status == 200, found
        return {str(s["name"]) for s in found}
