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
SABLIER_URL = "http://127.0.0.1:10000"
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
    app_image: str | None = (
        None  # the image of `shelfhold` alone (the rollback journey)
    )
    extra_env: dict[str, str] = field(
        default_factory=dict
    )  # E2E_PKI_DIR (the M2 overlay)

    def _command(self) -> list[str]:
        command = ["docker", "compose", "-p", PROJECT]
        for name in self.files:
            command += ["-f", str(E2E / name)]
        return command

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            **self.extra_env,
            **({"SHELFHOLD_APP_IMAGE": self.app_image} if self.app_image else {}),
        }

    def compose(
        self, *args: str, check: bool = True, timeout: int = 300
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(  # noqa: S603
            [*self._command(), *args], capture_output=True, text=True, check=False,
            timeout=timeout, env=self._env(),
        )  # fmt: skip
        if check and result.returncode:
            raise RuntimeError(
                f"docker compose {' '.join(args)} failed:\n{result.stderr}"
            )
        return result

    # -- what the administrator types -------------------------------------------------
    def shelfhold(
        self, *args: str, service: str = "shelfhold", env: dict[str, str] | None = None
    ) -> Cli:
        """`env`: variables of this one command (the restored instance of a restore journey)."""
        given = [item for k, v in (env or {}).items() for item in ("-e", f"{k}={v}")]
        done = self.compose(
            "exec", "-T", *given, service, "shelfhold", *args, check=False
        )
        return Cli(done.returncode, done.stdout, done.stderr)

    def put(self, path: str, text: str, *, service: str = "shelfhold") -> None:
        """A file the administrator brings (a secret, shelfhold.toml): written in the tmpfs."""
        subprocess.run(  # noqa: S603
            [*self._command(), "exec", "-T", service, "sh", "-c",
             f"umask 077; cat > {shlex.quote(path)}"],
            input=text, text=True, check=True, capture_output=True, timeout=60, env=self._env(),
        )  # fmt: skip

    def put_bytes(
        self, path: str, data: bytes, *, service: str = "shelfhold", old: bool = True
    ) -> None:
        """A file the administrator drops (binary); `old`: its date is far in the past, as a file
        that finished copying long ago (the import folder waits for a file to settle)."""
        subprocess.run(  # noqa: S603
            [*self._command(), "exec", "-T", service, "sh", "-c",
             f"umask 022; mkdir -p \"$(dirname {shlex.quote(path)})\"; cat > {shlex.quote(path)}"
             + (f"; touch -d '2020-01-01 00:00:00' {shlex.quote(path)}" if old else "")],
            input=data, check=True, capture_output=True, timeout=60, env=self._env(),
        )  # fmt: skip

    def read_bytes(self, path: str, *, service: str = "shelfhold") -> bytes:
        done = subprocess.run(  # noqa: S603
            [*self._command(), "exec", "-T", service, "cat", path],
            check=True, capture_output=True, timeout=60, env=self._env(),
        )  # fmt: skip
        return done.stdout

    def api(
        self,
        *steps: tuple[object, ...],
        mode: str = "session",
        user: tuple[str, str] | None = None,
    ) -> list[tuple[int, object]]:
        """Requests of the SPA, in one session: `(method, path[, body[, headers]])`. `mode`:
        `session` (login, then the CSRF header as the page sends it), `nocsrf` (the cookie alone, a
        forged cross-site request), `anon` (nothing), `bearer:<key>` (an API key). `user`: the
        `(username, password)` of the profile that signs in (the administrator by default)."""
        self.put("/tmp/api_client.py", API_CLIENT)
        who = user or (ADMIN_USER, ADMIN_PASSWORD)
        done = self.compose(
            "exec", "-T", "shelfhold", "python", "/tmp/api_client.py", json.dumps(steps), mode,
            who[0], who[1],
        )  # fmt: skip
        return [(int(code), body) for code, body in json.loads(done.stdout)]

    def create_admin(self) -> None:
        self.put("/tmp/admin.pw", ADMIN_PASSWORD + "\n")
        done = self.shelfhold("admin", "create", "--username", ADMIN_USER,
                              "--password-file", "/tmp/admin.pw")  # fmt: skip
        assert done.code == 0, done.err

    def egress(self) -> list[str]:
        """What left the network namespace of the stack (ENF-PRIV-001): nothing, ever."""
        logs = self.compose(
            "logs", "--no-color", "--no-log-prefix", "egress-watch"
        ).stdout
        return [line for line in logs.splitlines() if line.startswith("EGRESS")]

    def switch_app_image(self, image: str) -> None:
        """Another release on the same volumes: the container is recreated (the migration service
        runs first, as in a redeploy); the simulated servers and the volumes stay as they are."""
        self.compose("stop", "shelfhold")
        self.app_image = image
        # the simulated servers stay up: no `--wait` on their health (it flaps on a loaded host)
        self.compose("run", "--rm", "--no-deps", "-T", "shelfhold-migrate")
        self.compose("up", "-d", "--force-recreate", "--no-deps", "shelfhold")
        assert wait_for(lambda: self.shelfhold("status").code == 0, 180)

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

    def sablier(self) -> dict[str, list[dict[str, object]]]:
        return self.fake("sablier", SABLIER_URL + "/_fake/state")  # type: ignore[return-value]

    def suwayomi_do(self, action: str) -> None:
        """`publish_hidden`, `sleep`, `wake`, `expire`: what happens to the simulated Suwayomi."""
        self.fake("suwayomi", f"{SUWAYOMI_URL}/_fake/{action}", post=True)

    def child_account(
        self,
        name: str,
        library_id: int,
        *,
        more: tuple[int, ...] = (),
        age: int = -1,
        unknowns: bool = True,
    ) -> None:
        """A restricted Kavita account that reads some libraries, as a child of the household
        (`age`: the Kavita rating that caps it, -1 for none)."""
        libraries = "".join(f"&library={i}" for i in (library_id, *more))
        self.fake(
            "kavita",
            f"{KAVITA_URL}/_fake/child?name={name}{libraries}&age={age}&unknowns={int(unknowns)}",
            post=True,
        )

    def visible_to(self, name: str) -> list[str]:
        """The series a Kavita account can read (the simulated server's own rule)."""
        return self.fake("kavita", f"{KAVITA_URL}/_fake/visible?name={name}")  # type: ignore[return-value]

    def stat(self, path: str) -> tuple[int, int]:
        """`(inode, hard links)` of a file of the shelfhold container."""
        done = self.compose("exec", "-T", "shelfhold", "stat", "-c", "%i %h", path)
        inode, links = done.stdout.split()
        return int(inode), int(links)

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
        self.fake(
            "kavita", KAVITA_URL + "/_fake/index?folder=" + quote(folder), post=True
        )

    def suwayomi_category(
        self, name: str, *, exclude: bool, assign: tuple[str, ...]
    ) -> None:
        """A category in Suwayomi's library (`exclude`: its series skip the scheduled updates)."""
        from urllib.parse import quote

        query = f"name={quote(name)}" + ("&exclude=1" if exclude else "")
        query += "".join("&assign=" + quote(url) for url in assign)
        self.fake("suwayomi", SUWAYOMI_URL + "/_fake/category?" + query, post=True)

    def foreign_series(self, name: str) -> None:
        """Kavita holds a series called `name` that comes from another source than Shelfhold."""
        from urllib.parse import quote

        self.fake(
            "kavita",
            KAVITA_URL + "/_fake/foreign_series?name=" + quote(name),
            post=True,
        )

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
        assert wait_for(lambda: self.units_known() > 0, 120), (
            "la découverte n'a rien listé"
        )

    def follow_until(
        self, predicate: Callable[[], bool], seconds: float = 240.0
    ) -> bool:
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
        return sorted(
            line for line in done.stdout.splitlines() if line
        )  # names hold spaces

    def container_id(self, service: str = "shelfhold") -> str:
        return self.compose("ps", "-a", "-q", service).stdout.strip()


def wait_for(
    predicate: Callable[[], bool], seconds: float = 30.0, step: float = 1.0
) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (RuntimeError, ValueError, subprocess.SubprocessError):
            pass  # not up yet
        time.sleep(step)
    return False


# -- the HTTP side of the administrator (the SPA's own calls: session and CSRF) -------------------
ADMIN_USER = "admin"
ADMIN_PASSWORD = "synthetic-admin-password-1"  # noqa: S105 - a throwaway of the test stack
API_CLIENT = r"""
import http.cookiejar as cj, json, sys, urllib.request as u
steps, mode = json.loads(sys.argv[1]), sys.argv[2]
jar = cj.CookieJar()
opener = u.build_opener(u.HTTPCookieProcessor(jar))

def call(method, path, body=None, headers=None):
    request = u.Request("http://127.0.0.1:8080" + path, method=method, headers={
        "content-type": "application/json", **(headers or {})},
        data=None if body is None else json.dumps(body).encode())
    try:
        reply = opener.open(request, timeout=150)
        status, text = reply.status, reply.read().decode()
    except u.HTTPError as error:
        status, text = error.code, error.read().decode()
    try:
        return [status, json.loads(text) if text else None]
    except ValueError:
        return [status, text]

extra = {}
if mode in ("session", "nocsrf"):
    code, _ = call("POST", "/api/v1/auth/login", {"username": sys.argv[3], "password": sys.argv[4]})
    assert code == 200, code
    if mode == "session":
        extra["X-CSRF-Token"] = next(c.value for c in jar if c.name.endswith("csrf"))
elif mode.startswith("bearer:"):
    extra["Authorization"] = "Bearer " + mode[len("bearer:"):]
print(json.dumps([call(m, p, (rest[0] if rest else None), {**extra, **(rest[1] if len(rest) > 1 else {})})
                  for m, p, *rest in steps]))
"""
