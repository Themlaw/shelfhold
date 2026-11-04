"""Docker harness for the Suwayomi contract bench (spike S5, ENF-LEG-013).

Starts the pinned Suwayomi of ``e2e/suwayomi/compose.yaml`` on its internal network and
reaches it through the container's address on that network (the network has no published
port and no route out). Only Suwayomi's local source is fed, with synthetic CBZ.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
COMPOSE_FILE = ROOT / "e2e" / "suwayomi" / "compose.yaml"
SYNTHETIC = ROOT / "backend" / "tests" / "fixtures" / "synthetic"
LOCAL_SOURCE_ID = "0"
LOCAL_SOURCE_DIR = "Local source"  # downloads/mangas/<this>/<manga>/<chapter>.cbz
USER = "shelfhold"
PASSWORD = "contract-bench-only"  # noqa: S105 - throwaway credential of an isolated bench
PORT = 4567


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class Bench:
    mode: str
    data_dir: Path
    project: str
    password: str = PASSWORD
    base_url: str = ""
    token: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    # -- docker -----------------------------------------------------------------
    def _compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "SUWAYOMI_DATA": str(self.data_dir),
            "SUWAYOMI_AUTH_MODE": self.mode,
            "SUWAYOMI_USER": USER,
            "SUWAYOMI_PASSWORD": self.password,
        }
        return subprocess.run(  # noqa: S603
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", self.project, *args],  # noqa: S607
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def _address(self) -> str:
        cid = self._compose("ps", "-q", "suwayomi").stdout.strip()
        fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
        out = subprocess.run(  # noqa: S603
            ["docker", "inspect", "-f", fmt, cid],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    def start(self) -> None:
        self._compose("up", "-d", "--pull", "missing")
        self._wait_ready()

    def restart(self) -> None:
        self._compose("restart", "suwayomi")
        self._wait_ready()

    def exec_in_container(self, command: str) -> subprocess.CompletedProcess[str]:
        cid = self._compose("ps", "-q", "suwayomi").stdout.strip()
        return subprocess.run(  # noqa: S603
            ["docker", "exec", cid, "sh", "-c", command],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def logs(self) -> str:
        return self._compose("logs", "--no-color", "suwayomi").stdout

    def stop(self) -> None:
        self._compose("down", "-v", "--remove-orphans")

    def _wait_ready(self, timeout: float = 240) -> None:
        self.base_url = f"http://{self._address()}:{PORT}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"{self.base_url}/api/v1/settings/about", timeout=3)  # noqa: S310
            except urllib.error.HTTPError:
                return  # 401 in an authenticated mode: the server answers
            except OSError:
                time.sleep(2)
            else:
                return
        raise TimeoutError(f"Suwayomi not ready after {timeout}s\n{self.logs()[-3000:]}")

    # -- HTTP -------------------------------------------------------------------
    def auth_header(self, password: str | None = None) -> dict[str, str]:
        if self.mode == "basic_auth":
            raw = f"{USER}:{self.password if password is None else password}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        password: str | None = None,
    ) -> Response:
        all_headers = dict(headers or {})
        if authenticated:
            all_headers |= self.auth_header(password)
        req = urllib.request.Request(  # noqa: S310
            self.base_url + path, data=body, headers=all_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                return Response(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as err:
            return Response(err.code, dict(err.headers), err.read())

    def graphql(
        self, query: str, *, authenticated: bool = True, password: str | None = None
    ) -> Response:
        return self.request(
            "POST",
            "/api/graphql",
            body=json.dumps({"query": query}).encode(),
            headers={"Content-Type": "application/json"},
            authenticated=authenticated,
            password=password,
        )

    def gql(self, query: str) -> Any:
        """Run a query, require no GraphQL error, return ``data``."""
        resp = self.graphql(query)
        assert resp.status == 200, resp.body[:500]
        payload = resp.json()
        assert "errors" not in payload, str(payload["errors"])[:500]
        return payload["data"]

    def login(self) -> dict[str, str]:
        """ui_login: obtain the JWT pair and use the access token from now on."""
        data = self.graphql(
            f'mutation {{ login(input: {{username: "{USER}", password: "{self.password}"}}) '
            "{ accessToken refreshToken } }",
            authenticated=False,
        ).json()["data"]["login"]
        self.token = data["accessToken"]
        return {"access": data["accessToken"], "refresh": data["refreshToken"]}

    # -- fixtures ---------------------------------------------------------------
    def seed_local_library(self) -> None:
        """Series A (2 chapters) and Series B (1 chapter), from the synthetic generator."""
        local = self.data_dir / "local"
        for series, names in {
            "Series A": [("Ch 001", "comic_a_001.cbz"), ("Ch 002", "comic_a_002.cbz")],
            "Series B": [("Ch 001", "comic_a_001.cbz")],
        }.items():
            (local / series).mkdir(parents=True, exist_ok=True)
            for chapter, fixture in names:
                shutil.copyfile(SYNTHETIC / fixture, local / series / f"{chapter}.cbz")
        _open_permissions(self.data_dir)

    def seed_download(self, series: str, chapter: str, fixture: str = "comic_a_001.cbz") -> Path:
        """Place a CBZ where Suwayomi keeps a downloaded chapter.

        Suwayomi's downloader skips the local source (Downloader.kt), so a download can
        only be simulated by writing the file at the path the server looks up.
        """
        target = self.data_dir / "downloads" / "mangas" / LOCAL_SOURCE_DIR / series
        target.mkdir(parents=True, exist_ok=True)
        dest = target / f"{chapter}.cbz"
        shutil.copyfile(SYNTHETIC / fixture, dest)
        _open_permissions(self.data_dir)
        return dest


def _open_permissions(path: Path) -> None:
    """The container user is not the runner's user: let it read and write the bind mount."""
    path.chmod(0o777)
    for sub in ("local", "downloads"):
        root = path / sub
        if not root.is_dir():
            continue
        for item in [root, *root.rglob("*")]:
            if not item.is_symlink():
                item.chmod(0o777 if item.is_dir() else 0o666)
