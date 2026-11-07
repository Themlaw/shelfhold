"""The journey stack: the image under test, a simulated Kavita and a simulated Suwayomi, one
shared library volume (e2e/compose.yaml + e2e/journey.yaml).

Every step an administrator takes goes through the CLI of the running container, the same
`shelfhold` command as in production: no SQL, no file edited in the volumes.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

E2E = Path(__file__).resolve().parents[1]
PROJECT = os.environ.get("SHELFHOLD_E2E_PROJECT", "shelfhold-e2e-journey")
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
            [*command, "exec", "-T", service, "sh", "-c", f"umask 077; cat > {shlex.quote(path)}"],
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

    def adopted_series(self, name: str) -> None:
        """A series the administrator already has in Kavita's library: its CBZ (with a ComicInfo)
        written on the shared volume, then indexed by the simulated Kavita."""
        from urllib.parse import quote

        folder = str(self.kavita()["libraries"][0]["folders"][0]) + "/" + name  # type: ignore[index]
        code = (
            "import sys, zipfile, pathlib; d = pathlib.Path(sys.argv[1]); d.mkdir(parents=True, "
            "exist_ok=True); n = sys.argv[2]; "
            "z = zipfile.ZipFile(d / (n + ' - Ch.1.cbz'), 'w'); "
            "z.writestr('001.png', b'\\x89PNG\\r\\n\\x1a\\n' + n.encode()); "
            'z.writestr(\'ComicInfo.xml\', \'<?xml version="1.0" encoding="utf-8"?><ComicInfo>'
            "<Series>' + n + '</Series><LanguageISO>en</LanguageISO><Number>1</Number>"
            "</ComicInfo>'); z.close()"
        )
        self.compose("exec", "-T", "shelfhold", "python", "-c", code, folder, name)
        self.fake("kavita", KAVITA_URL + "/_fake/index?folder=" + quote(folder), post=True)

    def suwayomi_category(self, name: str, *, exclude: bool, assign: tuple[str, ...]) -> None:
        """A category in Suwayomi's library (`exclude`: its series skip the scheduled updates)."""
        from urllib.parse import quote

        query = f"name={quote(name)}" + ("&exclude=1" if exclude else "")
        query += "".join("&assign=" + quote(url) for url in assign)
        self.fake("suwayomi", SUWAYOMI_URL + "/_fake/category?" + query, post=True)

    def foreign_series(self, name: str) -> None:
        """Kavita holds a series called `name` that comes from another source than Shelfhold."""
        from urllib.parse import quote

        self.fake("kavita", KAVITA_URL + "/_fake/foreign_series?name=" + quote(name), post=True)

    def twin_titles(self) -> None:
        """The second series of the simulated Suwayomi takes the title of the first (en, fr)."""
        self.fake("suwayomi", SUWAYOMI_URL + "/_fake/twin", post=True)

    def waiting(self) -> dict[str, list[dict[str, object]]]:
        """`shelfhold status --json`: the waiting states, by state."""
        done = self.shelfhold("status", "--json")
        assert done.code == 0, done.err
        found: dict[str, list[dict[str, object]]] = {}
        for item in json.loads(done.out)["waiting"]:
            found.setdefault(str(item["state"]), []).append(item)
        return found

    def db_value(self, sql: str) -> str:
        """A read-only look at the database (an identifier to give a command, never a write)."""
        done = self.compose(
            "exec", "-T", "shelfhold", "python", "-c",
            "import sqlite3,sys; print(sqlite3.connect('file:/data/shelfhold.db?mode=ro', uri=True)"
            ".execute(sys.argv[1]).fetchone()[0])", sql,
        )  # fmt: skip
        return done.stdout.strip()

    def units_known(self) -> int:
        """Chapters the follow has listed so far (read-only look at the database, for waiting)."""
        done = self.compose(
            "exec", "-T", "shelfhold", "python", "-c",
            "import sqlite3; print(sqlite3.connect('file:/data/shelfhold.db?mode=ro', uri=True)"
            ".execute('select count(*) from unit').fetchone()[0])",
            check=False,
        )  # fmt: skip
        return int(done.stdout.strip() or 0) if done.returncode == 0 else 0

    def sql(self, query: str) -> list[tuple[object, ...]]:
        """A read-only look at the database."""
        code = (
            "import json, sqlite3; print(json.dumps(sqlite3.connect('file:/data/shelfhold.db?mode"
            "=ro', uri=True).execute(" + repr(query) + ").fetchall()))"
        )
        done = self.compose("exec", "-T", "shelfhold", "python", "-c", code)
        return [tuple(r) for r in json.loads(done.stdout)]

    def discover_series(self) -> None:
        """The first follow cycle: the series of the Suwayomi library get their Work and Edition
        and the chapters visible now become the baseline (nothing is asked). Returns once the
        chapters are listed, so what is published afterwards is new."""
        assert self.shelfhold("jobs", "run", "follow.cycle").code == 0
        assert wait_for(lambda: self.units_known() > 0, 120), "la découverte n'a rien listé"

    def follow_until(self, predicate: Callable[[], bool], seconds: float = 240.0) -> bool:
        """Ask for follow cycles until `predicate` holds. A download takes two looks of the
        service (asked in one cycle, collected in the next), as with Suwayomi's own queue."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            assert self.shelfhold("jobs", "run", "follow.cycle").code == 0
            if wait_for(predicate, 30):
                return True
        return False

    def library_files(self) -> list[str]:
        done = self.compose(
            "exec", "-T", "shelfhold", "find", "/library", "-type", "f", check=False
        )
        return sorted(line for line in done.stdout.splitlines() if line)  # names hold spaces

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
