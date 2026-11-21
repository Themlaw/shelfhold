"""Harness of the nightly against a real Komga (L44; EF-EXP-020, ENF-COMP-001).

The official image runs on an internal-only network (fixtures/komga/bootstrap/compose.yaml); the
suite reaches it through the container's address. Set-up goes through the API only (the claim of a
fresh install, an API key of the administrator, the libraries), then Shelfhold's own adapter is
used where Shelfhold would use it: the `KomgaClient`. Raw requests remain for what Shelfhold
never does (claim the server, create a key, create a library, read the series it holds). Synthetic
files only, no network but that internal one.
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
from urllib.parse import quote

from shelfhold.export.targets.komga import KomgaClient, KomgaKey
from shelfhold.headless.wiring import integration_fetcher
from shelfhold.infrastructure.clock import SystemClock

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "komga" / "bootstrap"
COMPOSE_FILE = FIXTURES / "compose.yaml"
BOOTSTRAP: dict[str, Any] = json.loads((FIXTURES / "bootstrap.json").read_text("utf-8"))
PORT = 25600

# The DTO of `POST /api/v1/libraries` as Komga's OpenAPI document writes it: every field is
# required, whatever the defaults of a screen. The scan is the test's to ask for, never the
# server's own schedule.
LIBRARY_DTO = {
    "name": "",  # of the call
    "root": "",  # of the call
    "importComicInfoBook": True,
    "importComicInfoSeries": True,
    "importComicInfoCollection": True,
    "importComicInfoReadList": True,
    "importComicInfoSeriesAppendVolume": True,
    "importEpubBook": True,
    "importEpubSeries": True,
    "importMylarSeries": False,
    "importLocalArtwork": True,
    "importBarcodeIsbn": False,
    "scanForceModifiedTime": False,
    "scanInterval": "DISABLED",
    "scanOnStartup": False,
    "scanCbx": True,
    "scanPdf": False,
    "scanEpub": False,
    "scanDirectoryExclusions": [],
    "repairExtensions": False,
    "convertToCbz": False,
    "emptyTrashAfterScan": False,
    "seriesCover": "FIRST",
    "hashFiles": True,
    "hashPages": False,
    "analyzeDimensions": False,
}  # fmt: skip


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


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


def comicinfo(series: str, number: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<ComicInfo>'
        f"<Series>{series}</Series><Number>{number}</Number>"
        "</ComicInfo>\n"
    )


def cbz(info: str | None = None, pages: int = 2) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index in range(pages):
            archive.writestr(f"{index + 1:03d}.png", png(shade=60 + index))
        if info is not None:
            archive.writestr("ComicInfo.xml", info)
    return buffer.getvalue()


@dataclass
class RealKomga:
    image: str
    library_dir: Path
    project: str = field(default_factory=lambda: f"nightly-komga-{uuid.uuid4().hex[:6]}")
    base_url: str = ""
    key: str = field(default="", repr=False)
    client: KomgaClient | None = None

    # -- docker -----------------------------------------------------------------------------
    def _compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "KOMGA_IMAGE": self.image, "KOMGA_LIBRARY": str(self.library_dir)}
        return subprocess.run(  # noqa: S603
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", self.project, *args],  # noqa: S607
            env=env, check=True, capture_output=True, text=True, timeout=600,
        )  # fmt: skip

    def _address(self) -> str:
        cid = self._compose("ps", "-q", "komga").stdout.strip()
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
                urllib.request.urlopen(f"{self.base_url}/api/v1/claim", timeout=3)  # noqa: S310
                break
            except (OSError, urllib.error.URLError):
                time.sleep(2)
        else:
            raise TimeoutError("Komga did not become ready\n" + self.logs()[-3000:])
        self._claim_admin()
        self._make_key()

    def stop(self) -> None:
        self._compose("down", "-v", "--remove-orphans", "-t", "5")

    def logs(self) -> str:
        return self._compose("logs", "--no-color", "komga").stdout

    # -- set-up ------------------------------------------------------------------------------
    def request(
        self, method: str, path: str, *, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )  # fmt: skip
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def _claim_admin(self) -> None:
        """The claim of a fresh install: the first account, an administrator of the server."""
        admin = BOOTSTRAP["admin"]
        status, user = self.request(
            "POST", "/api/v1/claim",
            headers={"X-Komga-Email": admin["email"], "X-Komga-Password": admin["password"]},
        )  # fmt: skip
        assert status == 200, f"the claim was refused ({status}): {user}"

    def _make_key(self) -> None:
        """The API key of the administrator, as the administrator makes one for Shelfhold."""
        status, made = self.request(
            "POST", "/api/v2/users/me/api-keys", body={"comment": BOOTSTRAP["key_comment"]},
            headers={"Authorization": f"Basic {self._basic()}"},
        )  # fmt: skip
        assert status == 200 and made["key"], made
        self.key = str(made["key"])
        self.client = KomgaClient(
            integration_fetcher(self.base_url), self.base_url, KomgaKey(self.key),
            clock=SystemClock(),
        )  # fmt: skip

    def _basic(self) -> str:
        import base64

        admin = BOOTSTRAP["admin"]
        pair = f"{admin['email']}:{admin['password']}"
        return base64.b64encode(pair.encode()).decode()

    # -- what the suite does ------------------------------------------------------------------
    def write(self, relative: str, content: bytes) -> Path:
        path = self.library_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def remote(self, relative: str = "") -> str:
        return f"{BOOTSTRAP['library_root']}/{relative}".rstrip("/")

    def create_library(self, name: str, root: str) -> str:
        """A library through the API, with the DTO Komga's document asks for: what the
        administrator does once, before Shelfhold ever talks to the server."""
        status, made = self.request(
            "POST", "/api/v1/libraries", body={**LIBRARY_DTO, "name": name, "root": root},
            headers={"Authorization": f"Basic {self._basic()}"},
        )  # fmt: skip
        assert status == 200 and made["id"], (status, made)
        return str(made["id"])

    def series(self, term: str) -> dict[str, dict[str, Any]]:
        """The series a search of Komga's own API finds, by name (a series in the trash is still
        answered, `deleted`: what the emptying of the trash is judged by)."""
        status, found = self.request(
            "GET", f"/api/v1/series?search={quote(term)}&unpaged=true",
            headers={"Authorization": f"Basic {self._basic()}"},
        )  # fmt: skip
        assert status == 200, (status, found)
        return {str(row["name"]): dict(row) for row in found["content"]}

    def wait_series(
        self, term: str, ready: Callable[[dict[str, dict[str, Any]]], bool], what: str
    ) -> dict[str, dict[str, Any]]:
        deadline = time.monotonic() + float(BOOTSTRAP["scan_timeout_s"])
        found: dict[str, dict[str, Any]] = {}
        while time.monotonic() < deadline:
            found = self.series(term)
            if ready(found):
                return found
            time.sleep(3)
        raise AssertionError(f"{what}: not indexed in time; saw {sorted(found)}")
