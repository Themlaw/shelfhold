"""M6 L05 (EF-SRC-016, EF-SRC-019, ENF-SEC-009, D-P1), after the adversarial review: what a plugin's
process may do to the companion that started it.

A plugin runs under the companion's own uid, so the container's uid does not separate them. What
does: Landlock (a plugin writes in a scratch folder of its own and nowhere else, neither beside the
socket nor on the plugins volume, and opens no TCP connection where the kernel can say so), the
subreaper (what a plugin starts and abandons is killed with its connection), a companion that is
not dumpable, one uid served (the core's), and a socket that must be a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import textwrap
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host.adapters.rpc.runner_socket import (
    RunnerRefused,
    RunnerUnavailable,
    connect_runner,
)
from shelfhold.plugin_host.rpc.host_side import RpcSession
from shelfhold.plugin_host.rpc.protocol import RpcLimits
from shelfhold.plugin_host.runner import confine
from shelfhold.plugin_host.runner.selfcheck import (
    DOCKER_SOCKETS,
    Observation,
    SelfCheckFailed,
    observe,
    problems_of,
    stray_entries,
)
from shelfhold.plugin_host.runner.server import RunnerConfig, RunnerServer
from tests.plugin_guard_env import plugin_wheel

FAKES = Path(__file__).resolve().parent.parent / "fakes"
CORE_UID, RUNNER_UID = 1000, 1001
CLEAN = Observation(
    uid=RUNNER_UID, visible_paths=(), interfaces=("lo",), outbound_possible=False,
    root_read_only=True,
)  # fmt: skip


def run[T](make: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(asyncio.wait_for(make(), 90))


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="rc", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def install(root: Path, name: str, source: str) -> None:
    site = root / "plugins" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    shutil.copy(FAKES / "rpc_plugin.py", site / "rpc_plugin.py")
    (site / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")


def server_in(root: Path, *, peer: int = CORE_UID, **config: object) -> RunnerServer:
    return RunnerServer(
        RunnerConfig(socket_path=root / "runner.sock", plugins_root=root / "plugins", **config),  # type: ignore[arg-type]
        own_uid=RUNNER_UID,
        peer_uid=lambda _sock: peer,
        observe=lambda: CLEAN,
    )


async def session(server: RunnerServer, key: str, entry: str) -> RpcSession:
    limits = RpcLimits()
    channel = await connect_runner(
        server.config.socket_path, key=key, entry=entry, limits=limits, core_uid=CORE_UID
    )
    return await RpcSession.open(channel, key=key, limits=limits)


async def search(s: RpcSession, q: str) -> list[str]:
    page = await s.call("source.search", {"q": q, "filters": {}, "cursor": None}, fetcher=None)
    return [item.title for item in page.items]


def prctl_get(option: int) -> int:
    return int(ctypes.CDLL(None).prctl(option, 0, 0, 0, 0))


BOUND = """
import json, os, socket
from rpc_plugin import Plugin, _ref

class Bound(Plugin):
    async def search(self, f, q, filters, cursor, *, sub=None):
        from shelfhold_sdk.models import Page
        target, site = json.loads(q)
        out = []

        def attempt(name, action):
            try:
                action()
                out.append(f"{name}:ok")
            except OSError as exc:
                out.append(f"{name}:{type(exc).__name__}")

        folder = os.path.dirname(target)
        attempt("unlink", lambda: os.unlink(target))
        attempt("rename", lambda: os.rename(target, target + ".moved"))
        attempt("bind", lambda: socket.socket(socket.AF_UNIX).bind(target + ".x"))
        attempt("create", lambda: open(os.path.join(folder, "new"), "w"))
        attempt("link", lambda: os.symlink("/etc", target + ".l"))
        attempt("plant", lambda: open(os.path.join(site, "evil.py"), "w"))
        attempt("scratch", lambda: open(os.path.join(os.environ["TMPDIR"], "ok"), "w").close())
        attempt("devnull", lambda: open(os.devnull, "w").close())
        attempt("tcp", lambda: socket.create_connection(("127.0.0.1", 9), timeout=1))
        return Page(items=[_ref(x) for x in out])

def make():
    return Bound()
"""


@pytest.mark.req("EF-SRC-016", "EF-SRC-019", "ENF-SEC-009")
def test_a_plugin_writes_in_its_scratch_folder_and_nowhere_else(short_dir: Path) -> None:
    """What the review's first proof-of-concept does (replace the companion's socket, plant a file
    on the plugins volume): refused, by the kernel, for the plugin's process; its scratch folder
    and `/dev/null` are the only places it writes."""
    install(short_dir, "bound", BOUND)
    target = short_dir / "runner.sock"
    site = short_dir / "plugins" / "site-packages"

    async def scenario() -> dict[str, str]:
        server = server_in(short_dir)
        await server.start()
        try:
            before = target.stat().st_ino
            s = await session(server, "rpc-demo", "bound:make")
            seen = dict(item.split(":", 1) for item in await search(s, f'["{target}", "{site}"]'))
            await s.close()
            assert target.stat().st_ino == before  # the same socket, the same inode
            return seen
        finally:
            await server.close()

    seen = run(scenario)
    for name in ("unlink", "rename", "bind", "create", "link", "plant"):
        assert seen[name] == "PermissionError", (name, seen)
    assert seen["scratch"] == "ok" and seen["devnull"] == "ok"
    if confine.landlock_abi() >= 4:  # the kernel can refuse TCP: it does, for this process
        assert seen["tcp"] == "PermissionError", seen
    assert not (short_dir / "new").exists() and not (site / "evil.py").exists()
    assert not list(short_dir.glob("runner.sock.*"))


ESCAPE = """
import os, time
from rpc_plugin import Plugin, _ref

def _spawn(detach):
    pidfile = os.path.join(os.environ["TMPDIR"], "child.pid")
    if os.fork() == 0:
        if detach:
            os.setsid()
        if os.fork() == 0 or not detach:
            with open(pidfile, "w") as out:
                out.write(str(os.getpid()))
            time.sleep(120)
        os._exit(0)
    for _ in range(100):
        if os.path.exists(pidfile) and open(pidfile).read():
            break
        time.sleep(0.05)
    if detach:
        os.wait()

class Spawns(Plugin):
    async def search(self, f, q, filters, cursor, *, sub=None):
        from shelfhold_sdk.models import Page
        pidfile = os.path.join(os.environ["TMPDIR"], "child.pid")
        return Page(items=[_ref(open(pidfile).read())])

def leaves():
    _spawn(True)  # a grandchild in a session of its own, whose parent is gone
    return Spawns()

def stays():
    _spawn(False)  # a plain child, in the plugin's own group
    return Spawns()
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def wait_gone(server: RunnerServer, key: str) -> None:
    for _ in range(100):
        if key not in server.pids():
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_what_a_plugin_abandons_dies_with_its_connection_and_only_then(short_dir: Path) -> None:
    """The review's second proof-of-concept: a grandchild that leaves the plugin's session (a
    double fork and `setsid()`) is adopted by the companion, which kills it when the connection
    ends. A child that stays beneath a plugin that is still connected is left alone."""
    install(short_dir, "esc", ESCAPE)
    pids: list[int] = []

    async def scenario() -> None:
        server = server_in(short_dir)
        await server.start()
        try:
            leaver = await session(server, "rpc-demo", "esc:leaves")
            keeper = await session(server, "rpc-demo-2", "esc:stays")
            gone = int((await search(leaver, "q"))[0])
            kept = int((await search(keeper, "q"))[0])
            pids.extend([gone, kept])
            assert alive(gone) and alive(kept)
            await leaver.close()
            await wait_gone(server, "rpc-demo")
            assert not alive(gone), "a process the plugin abandoned outlived its connection"
            assert alive(kept), "the sweep killed what lives beneath a plugin still connected"
            await keeper.close()
            await wait_gone(server, "rpc-demo-2")
            assert not alive(kept)
        finally:
            await server.close()

    try:
        run(scenario)
    finally:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.req("EF-SRC-016", "EF-SRC-019", "ENF-SEC-009")
def test_the_companion_is_not_dumpable_and_gives_the_flags_back(short_dir: Path) -> None:
    dumpable = prctl_get(3)  # the test runner's own flag, whatever it is

    async def scenario() -> tuple[int, int]:
        server = server_in(short_dir)
        (short_dir / "plugins").mkdir()
        await server.start()
        inside = prctl_get(3)
        flag = ctypes.c_int(0)
        ctypes.CDLL(None).prctl(37, ctypes.byref(flag), 0, 0, 0)
        await server.close()
        return inside, flag.value

    inside, reaper = run(scenario)
    assert inside == 0 and reaper == 1  # not dumpable, and the subreaper, while it listens
    assert prctl_get(3) == dumpable  # and the process is as it was once it has stopped


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_the_companion_serves_the_core_uid_only(short_dir: Path) -> None:
    """`_admit` takes the uid the core was told to have (`--core-uid`), not any uid but the
    companion's: another user of the host who reaches the socket is refused."""
    install(short_dir, "bound", BOUND)

    async def scenario() -> None:
        server = server_in(short_dir, peer=4242)
        await server.start()
        try:
            with pytest.raises(RunnerRefused, match="only the core's uid 1000"):
                await connect_runner(
                    server.config.socket_path, key="rpc-demo", entry="bound:make",
                    limits=RpcLimits(), core_uid=4242,
                )  # fmt: skip
            assert server.pids() == {}
        finally:
            await server.close()

    run(scenario)

    # another core uid, configured: served
    async def configured() -> None:
        server = server_in(short_dir, peer=4242, core_uid=4242)
        await server.start()
        try:
            s = await session_as(server, 4242)
            await s.close()
        finally:
            await server.close()

    async def session_as(server: RunnerServer, uid: int) -> RpcSession:
        channel = await connect_runner(
            server.config.socket_path, key="rpc-demo", entry="bound:make",
            limits=RpcLimits(), core_uid=uid,
        )  # fmt: skip
        return await RpcSession.open(channel, key="rpc-demo", limits=RpcLimits())

    run(configured)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_the_socket_is_world_connectable_and_the_umask_is_left_alone(short_dir: Path) -> None:
    (short_dir / "plugins").mkdir()
    old = os.umask(0o027)
    try:

        async def scenario() -> int:
            server = server_in(short_dir)
            await server.start()
            try:
                seen = stat.S_IMODE((short_dir / "runner.sock").stat().st_mode)
                assert os.umask(0o027) == 0o027  # not changed behind the process's back
                return seen
            finally:
                await server.close()

        assert run(scenario) == 0o666
    finally:
        os.umask(old)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_the_core_connects_to_a_socket_and_to_nothing_else(short_dir: Path) -> None:
    """A link (to the CLI's socket, say) or a file in the place of `runner.sock` is not connected
    to: nothing is sent."""
    real = short_dir / "other.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(real))
    listener.listen()
    (short_dir / "link.sock").symlink_to(real)
    (short_dir / "file.sock").write_text("x")

    async def attempt(name: str) -> str:
        with pytest.raises(RunnerUnavailable) as raised:
            await connect_runner(
                short_dir / name, key="rpc-demo", entry="a:b", limits=RpcLimits(), timeout=2
            )
        return str(raised.value)

    try:
        assert "not a socket" in run(lambda: attempt("link.sock"))
        assert "not a socket" in run(lambda: attempt("file.sock"))
        assert "not reachable" in run(lambda: attempt("absent.sock"))
        listener.settimeout(0.2)
        with pytest.raises(TimeoutError):  # nothing connected to the socket behind the link
            listener.accept()
    finally:
        listener.close()


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_a_plugin_is_not_started_where_landlock_cannot_bind_it(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(short_dir, "bound", BOUND)

    def none(_writable: object) -> object:
        raise OSError("the kernel offers no Landlock")

    monkeypatch.setattr(confine, "Ruleset", none)

    async def scenario() -> None:
        server = server_in(short_dir)
        await server.start()
        try:
            with pytest.raises(RunnerRefused, match="could not be started"):
                await session(server, "rpc-demo", "bound:make")
            assert server.pids() == {} and not list(Path(tempfile.gettempdir()).glob("plugin-*x"))
        finally:
            await server.close()

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_the_self_check_wants_landlock_and_a_plugins_volume_with_nothing_else(
    tmp_path: Path,
) -> None:
    assert problems_of(CLEAN) == []  # a description that says nothing of Landlock is not refused
    assert problems_of(replace(CLEAN, landlock_abi=4)) == []
    refused = problems_of(replace(CLEAN, landlock_abi=0))
    assert len(refused) == 1 and "Landlock" in refused[0]
    odd = problems_of(replace(CLEAN, stray_entries=("shelfhold.db", "library")))
    assert len(odd) == 1 and "shelfhold.db" in odd[0] and "library" in odd[0]
    # what a volume holds, looked at: the wheels, the core's state, what is being installed
    assert stray_entries(tmp_path / "absent") == ()
    for name in ("site-packages", "state.json", ".staging-01ABC"):
        (tmp_path / name).mkdir()
    assert stray_entries(tmp_path) == ()
    (tmp_path / "data.db").write_text("x")
    assert stray_entries(tmp_path) == ("data.db",)
    seen = observe(paths=(), plugins_root=tmp_path, probe=("127.0.0.1", 1))
    assert seen.stray_entries == ("data.db",) and (seen.landlock_abi or 0) >= 0


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_the_self_check_sees_the_engine_socket(tmp_path: Path) -> None:
    """A mounted `docker.sock` is the host's, however it came to be there."""
    assert set(DOCKER_SOCKETS) == {"/var/run/docker.sock", "/run/docker.sock"}
    fake = tmp_path / "docker.sock"
    fake.write_text("")
    seen = observe(paths=(str(fake),), plugins_root=tmp_path, probe=("127.0.0.1", 1))
    assert seen.visible_paths == (str(fake),)
    with pytest.raises(SelfCheckFailed, match="can see"):
        raise SelfCheckFailed(problems_of(seen))


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_the_process_tree_helpers_find_kill_and_collect() -> None:
    parent = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 60 & wait"],
    )
    try:
        for _ in range(100):
            if confine.descendants_of(parent.pid):
                break
            time.sleep(0.05)
        assert confine.descendants_of(parent.pid)
        assert parent.pid in confine.children_of(os.getpid())
        assert parent.pid in confine.orphans(set())
        assert confine.orphans({parent.pid}) == set()  # what we started is not an orphan
        confine.kill_tree(parent.pid)
        parent.wait(timeout=10)
        assert not confine.descendants_of(parent.pid)
        confine.kill_tree(parent.pid)  # nothing left: nothing happens
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(parent.pid, signal.SIGKILL)
    confine.reap([parent.pid, 999999999])  # collected already, or never ours: no error


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_a_ruleset_needs_the_kernel_to_offer_landlock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(confine, "landlock_abi", lambda: 0)
    with pytest.raises(OSError, match="no Landlock"):
        confine.Ruleset([tmp_path])
    monkeypatch.undo()
    assert confine.landlock_abi() >= 1  # the machines that run the suite offer it
    rules = confine.Ruleset([tmp_path])
    rules.close()
    rules.close()  # twice: nothing
    with pytest.raises(OSError):
        confine.Ruleset([tmp_path / "absent"])  # a folder that is not there cannot be granted


@pytest.mark.req("EF-SRC-019", "ENF-SEC-009")
def test_the_guard_refuses_a_wheel_that_shadows_the_standard_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every plugin of a companion imports from the same `site-packages`, first on `sys.path`: a
    wheel with a `json` of its own would replace the standard one for all of them."""
    monkeypatch.chdir(tmp_path)
    layout = guard.Layout.of(tmp_path / "data")
    for module in ("json", "asyncio", "sitecustomize"):
        wheel = plugin_wheel(tmp_path, f"shadow-{module}", "demo", tmp_path / "p.jsonl", module)
        with pytest.raises(guard.PluginInstallError):
            guard.install_wheel(layout, wheel.path, wheel.sha256)
    assert not (layout.site / "json").exists()
