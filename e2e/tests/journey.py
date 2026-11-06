"""The journey stack: the image under test, a simulated Kavita and a simulated Suwayomi, one
shared library volume (e2e/compose.yaml + e2e/journey.yaml).

Every step an administrator takes goes through the CLI of the running container, the same
`shelfhold` command as in production: no SQL, no file edited in the volumes.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

E2E = Path(__file__).resolve().parents[1]
PROJECT = "shelfhold-e2e-journey"
KAVITA_URL = "http://127.0.0.1:5000"
SUWAYOMI_URL = "http://127.0.0.1:4567"
KAVITA_USER = "shelfhold"
KAVITA_PASSWORD = "synthetic-kavita-password"  # noqa: S105 - the simulated server's own
SUWAYOMI_SECRET = "shelfhold:synthetic-test-password"  # noqa: S105
PATH_MAP = [{"local": "/library", "remote": "/library", "mode": "rw"}]
FAKE_GET = "import urllib.request as u,sys; sys.stdout.write(u.urlopen(sys.argv[1], timeout=5).read().decode())"
FAKE_POST = (
    "import urllib.request as u; "
    "u.urlopen(u.Request(__import__('sys').argv[1], data=b'{}', method='POST'), timeout=5)"
)


@dataclass
class Cli:
    code: int
    out: str
    err: str


@dataclass
class Journey:
    files: list[str] = field(default_factory=lambda: ["compose.yaml", "journey.yaml"])

    def compose(
        self, *args: str, check: bool = True, timeout: int = 300
    ) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose", "-p", PROJECT]
        for name in self.files:
            command += ["-f", str(E2E / name)]
        result = subprocess.run(  # noqa: S603
            [*command, *args], capture_output=True, text=True, check=False, timeout=timeout
        )
        if check and result.returncode:
            raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{result.stderr}")
        return result

    # -- what the administrator types -------------------------------------------------
    def shelfhold(self, *args: str, service: str = "shelfhold") -> Cli:
        done = self.compose("exec", "-T", service, "shelfhold", *args, check=False)
        return Cli(done.returncode, done.stdout, done.stderr)

    def put(self, path: str, text: str, *, service: str = "shelfhold") -> None:
        """A file the administrator brings (a secret, shelfhold.toml): written in the tmpfs."""
        command = ["docker", "compose", "-p", PROJECT]
        for name in self.files:
            command += ["-f", str(E2E / name)]
        subprocess.run(  # noqa: S603
            [*command, "exec", "-T", service, "sh", "-c", f"umask 077; cat > {path}"],
            input=text,
            text=True,
            check=True,
            capture_output=True,
            timeout=60,
        )

    def secrets(self) -> None:
        self.put("/tmp/kavita.pw", KAVITA_PASSWORD + "\n")
        self.put("/tmp/suwayomi.secret", SUWAYOMI_SECRET + "\n")

    # -- the simulated servers' window (never used by Shelfhold) ----------------------
    def fake(self, service: str, url: str, *, post: bool = False) -> dict[str, object]:
        code = FAKE_POST if post else FAKE_GET
        done = self.compose("exec", "-T", "fake-" + service, "python", "-c", code, url)
        return {} if post else json.loads(done.stdout)

    def kavita(self) -> dict[str, object]:
        return self.fake("kavita", KAVITA_URL + "/_fake/state")

    def suwayomi(self) -> dict[str, object]:
        return self.fake("suwayomi", SUWAYOMI_URL + "/_fake/state")

    def publish_upstream(self) -> None:
        self.fake("suwayomi", SUWAYOMI_URL + "/_fake/publish", post=True)

    def units_known(self) -> int:
        """Chapters the follow has listed so far (read-only look at the database, for waiting)."""
        done = self.compose(
            "exec", "-T", "shelfhold", "python", "-c",
            "import sqlite3; print(sqlite3.connect('file:/data/shelfhold.db?mode=ro', uri=True)"
            ".execute('select count(*) from unit').fetchone()[0])",
            check=False,
        )  # fmt: skip
        return int(done.stdout.strip() or 0) if done.returncode == 0 else 0

    def discover_series(self) -> None:
        """The first follow cycle: the series of the Suwayomi library get their Work and Edition
        and the chapters visible now become the baseline (nothing is asked). Returns once the
        chapters are listed, so what is published afterwards is new."""
        assert self.shelfhold("jobs", "run", "follow.cycle").code == 0
        assert wait_for(lambda: self.units_known() > 0, 120), "la découverte n'a rien listé"

    def library_files(self) -> list[str]:
        done = self.compose(
            "exec", "-T", "shelfhold", "find", "/library", "-type", "f", check=False
        )
        return sorted(done.stdout.split())

    def container_id(self, service: str = "shelfhold") -> str:
        return self.compose("ps", "-a", "-q", service).stdout.strip()


def wait_for(predicate: Callable[[], bool], seconds: float = 30.0, step: float = 1.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (RuntimeError, ValueError, subprocess.SubprocessError):
            pass  # not up yet
        time.sleep(step)
    return False
