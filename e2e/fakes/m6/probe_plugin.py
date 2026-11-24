"""A source plugin that tells what its own process sees (M6 L05). Only the SDK and the standard
library: it is installed on the volume the companion reads and runs in the companion's subprocess.
`search` answers with one reference whose title is a JSON document. By its query:

- `probe`: the uid, the four paths the isolation check looks for (when they are visible), the names
  of its environment, the interfaces and what the kit's isolation check says of it;
- `write`: what happens when it writes where it must not (the socket's folder, the socket itself,
  the plugins volume) and where it may (its scratch folder);
- `kit`: the kit, in isolated mode and on what this process really sees, played on a plugin the
  repository ships (the Project Gutenberg source, on a catalogue made up here);
- `escape`: it starts a process in a session of its own, whose parent is gone, and says its pid;
- `modes`: it tries to change the modes of the socket and of its folder, and to leave its process
  group (`setsid`, `setpgid`), and says what it got and whether it is in its group still;
- `hop:<token>`: it starts a process that forks again every few milliseconds (a new pid each time,
  its parent gone), which answers on an abstract unix socket named by the token while it lives.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from typing import Any

from shelfhold_sdk.models import API_VERSION, Cursor, Page, PluginKind, RateLimit
from shelfhold_sdk.refs import SeriesRef
from shelfhold_sdk.source import Capability, SourceManifest
from shelfhold_sdk.testing.isolation import observe_sandbox, run_isolation_check


def _outbound() -> bool:
    probe = socket.socket()  # noqa: TID251 - a connection that must fail
    probe.settimeout(1.0)
    try:
        probe.connect(("192.0.2.1", 80))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _title(seen: dict[str, Any], cursor: Cursor | None = None) -> Page[SeriesRef]:
    ref = SeriesRef(source_key="e2e-probe", remote_id="1", title=json.dumps(seen))
    return Page(items=[ref], next=cursor)


def _writes() -> dict[str, Any]:
    """Each attempt, by name: `ok`, or the name of the error the kernel gave."""
    out: dict[str, Any] = {}

    def attempt(name: str, action: Any) -> None:
        try:
            action()
            out[name] = "ok"
        except OSError as exc:
            out[name] = type(exc).__name__

    folder = "/plugin-rpc"
    attempt("create_in_socket_folder", lambda: open(f"{folder}/planted", "w").close())
    attempt("unlink_socket", lambda: os.unlink(f"{folder}/runner.sock"))
    attempt(
        "rename_socket", lambda: os.rename(f"{folder}/runner.sock", f"{folder}/moved")
    )
    attempt("link_socket", lambda: os.symlink("/data/shelfhold.sock", f"{folder}/link"))
    attempt(
        "bind_in_socket_folder",
        lambda: socket.socket(socket.AF_UNIX).bind(f"{folder}/x"),
    )  # noqa: TID251
    attempt(
        "plant_in_plugins",
        lambda: open("/plugins/site-packages/planted.py", "w").close(),
    )
    attempt("write_root", lambda: open("/planted", "w").close())
    attempt(
        "scratch", lambda: open(os.path.join(os.environ["TMPDIR"], "ok"), "w").close()
    )
    return out


def _modes() -> dict[str, Any]:
    out: dict[str, Any] = {}

    def attempt(name: str, action: Any) -> None:
        try:
            action()
            out[name] = "ok"
        except OSError as exc:
            out[name] = type(exc).__name__

    attempt("chmod_socket", lambda: os.chmod("/plugin-rpc/runner.sock", 0o600))
    attempt("chmod_folder", lambda: os.chmod("/plugin-rpc", 0o700))
    attempt("chown_socket", lambda: os.chown("/plugin-rpc/runner.sock", os.getuid(), os.getgid()))
    attempt("setxattr", lambda: os.setxattr(os.environ["TMPDIR"], "user.x", b"1"))
    group, session = os.getpgrp(), os.getsid(0)
    attempt("setsid", os.setsid)
    attempt("setpgid", lambda: os.setpgid(0, 0))
    out["same_group"] = (os.getpgrp(), os.getsid(0)) == (group, session)
    return out


def _hop(token: str) -> bool:
    """A process that makes a new pid every few milliseconds, its parent gone each time (it
    tries `setsid` first, which does nothing here), and answers on an abstract socket."""
    if os.fork() == 0:
        with contextlib.suppress(OSError):
            os.setsid()
        if os.fork() == 0:
            os.closerange(0, 4096)
            held = socket.socket(socket.AF_UNIX)  # noqa: TID251 - the probe's own listener
            held.bind("\0" + token)
            held.listen(8)
            held.settimeout(0.02)
            end = time.monotonic() + 120
            while time.monotonic() < end:
                with contextlib.suppress(OSError):
                    held.accept()[0].close()
                try:
                    if os.fork() != 0:
                        os._exit(0)  # the parent goes: a new pid, adopted by the companion
                except OSError:
                    pass
            os._exit(0)
        os._exit(0)
    os.wait()
    return True


def _escape() -> int:
    """A grandchild in a session of its own, its parent gone: only the companion adopts it."""
    pidfile = os.path.join(os.environ["TMPDIR"], "escaped.pid")
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            with open(pidfile, "w") as out:
                out.write(str(os.getpid()))
            time.sleep(600)
        os._exit(0)
    for _ in range(100):
        if os.path.exists(pidfile) and open(pidfile).read():
            break
        time.sleep(0.05)
    os.wait()
    return int(open(pidfile).read())


KIT_CATALOG = "https://catalog.example.org/cache/epub/feeds/pg_catalog.csv"
KIT_MIRROR = "https://mirror.example.net/gutenberg"
KIT_CSV = (
    "Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
    '1001,Text,2001-01-01,A Study in Test,en,"Doe, Jane, 1850-1920",Tests,PR,\n'
    '3001,Text,2002-02-02,The Protected Tale,en,"Roe, Richard, 1900-1960",Tests,PR,\n'
)


async def _kit(core_uid: int) -> dict[str, Any]:
    from shelfhold_sdk.fetcher import Response
    from shelfhold_sdk.testing import FakeFetcher, run_source_conformance

    from shelfhold.plugin_host.adapters.native.gutenberg import GutenbergSource

    fetcher = FakeFetcher(
        {("GET", KIT_CATALOG): Response(status=200, body=KIT_CSV.encode())},
        settings={"catalog_url": KIT_CATALOG, "mirror_url": KIT_MIRROR},
    )
    report = await run_source_conformance(
        GutenbergSource(),
        fetcher,
        reinstall=GutenbergSource,
        isolated=True,
        core_uid=core_uid,
    )  # no sandbox is given: what the kit checks is what this process really sees
    return {
        "ok": report.ok,
        "failures": [f"{c.name}: {c.detail}" for c in report.failures],
        "names": sorted(c.name for c in report.checks),
    }


class Plugin:
    manifest = SourceManifest(
        key="e2e-probe",
        kind=PluginKind.SOURCE,
        version="1.0.0",
        api=API_VERSION,
        name="Probe",
        langs=["en"],
        content_kinds={"text"},
        url_patterns=[],
        capabilities=Capability.SEARCH,
        rate_limit=RateLimit(requests_per_second=1.0),
    )

    def filters(self) -> list[Any]:
        return []

    async def search(
        self,
        f: Any,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        core_uid = int(filters.get("core_uid", -1))
        if q == "write":
            return _title(_writes())
        if q == "kit":
            return _title(await _kit(core_uid))
        if q == "escape":
            return _title({"pid": _escape()})
        if q == "modes":
            return _title(_modes())
        if q.startswith("hop:"):
            return _title({"started": _hop(q[4:])})
        view = observe_sandbox()
        report = run_isolation_check(view, core_uid=core_uid)
        seen = {
            "uid": os.getuid(),
            "visible": list(view.visible),
            "env_names": sorted(os.environ),
            # (not a read of `/sys/class/net`: a plugin reads no file but the ones it needs)
            "interfaces": sorted(name for _, name in socket.if_nameindex()),
            "outbound": _outbound(),
            "isolation": {c.name: c.ok for c in report.checks},
        }
        return _title(seen, cursor)


def make() -> Plugin:
    return Plugin()
