"""M6 L05, adversarial review (EF-SRC-016, EF-SRC-019, ENF-SEC-009, D-P1): what a plugin's process
can do to the companion that started it.

In the companion's container the plugins run under the companion's own uid (1001), and the socket
folder `/plugin-rpc` belongs to that uid: whatever the companion may do there, a plugin may do
too. The tests play the companion in this process with real subprocesses, as
`test_plugin_runner.py` does; the plugin and the companion share a uid here exactly as they do in
the container, so what a plugin manages here it manages there.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import socket
import stat
import tempfile
import textwrap
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

from shelfhold.plugin_host.adapters.rpc.runner_socket import connect_runner
from shelfhold.plugin_host.rpc.host_side import RpcSession
from shelfhold.plugin_host.rpc.protocol import RpcLimits
from shelfhold.plugin_host.runner.selfcheck import Observation
from shelfhold.plugin_host.runner.server import RunnerConfig, RunnerServer

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
    path = Path(tempfile.mkdtemp(prefix="rv", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def install(root: Path, name: str, source: str) -> None:
    site = root / "plugins" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    shutil.copy(FAKES / "rpc_plugin.py", site / "rpc_plugin.py")
    (site / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")


def server_in(root: Path) -> RunnerServer:
    return RunnerServer(
        RunnerConfig(
            socket_path=root / "runner.sock",
            plugins_root=root / "plugins",
            max_tasks=1 << 20,  # the uid of a test host does other work: the plugins fork here
        ),
        own_uid=RUNNER_UID,
        peer_uid=lambda _sock: CORE_UID,
        observe=lambda: CLEAN,
    )


async def session(server: RunnerServer, key: str, entry: str, timeout: float = 15.0) -> RpcSession:
    limits = RpcLimits()
    channel = await connect_runner(
        server.config.socket_path,
        key=key,
        entry=entry,
        limits=limits,
        core_uid=CORE_UID,
        timeout=timeout,
    )
    return await RpcSession.open(channel, key=key, limits=limits)


async def search(s: RpcSession, q: str) -> list[str]:
    page = await s.call("source.search", {"q": q, "filters": {}, "cursor": None}, fetcher=None)
    return [item.title for item in page.items]


HIJACK = """
import os, socket, json
from rpc_plugin import Plugin, _ref

class Hijack(Plugin):
    async def search(self, f, q, filters, cursor, *, sub=None):
        from shelfhold_sdk.models import Page
        target = q
        out = []

        def attempt(name, action):
            try:
                action()
                out.append(f"{name}:ok")
            except OSError as exc:
                out.append(f"{name}:{type(exc).__name__}")

        def take_over():
            os.unlink(target)
            held = socket.socket(socket.AF_UNIX)
            held.bind(target)
            held.listen(8)
            globals()["_held"] = held  # the core's next connection reaches this plugin

        attempt("take_over", take_over)
        return Page(items=[_ref(x) for x in out])

def make():
    return Hijack()
"""


@pytest.mark.req("EF-SRC-016", "EF-SRC-019", "ENF-SEC-009")
def test_a_plugin_cannot_take_the_runner_socket_over(short_dir: Path) -> None:
    """A plugin unlinks `runner.sock` (its folder is its uid's) and binds its own: the next plugin
    the core asks for would be greeted by the first plugin, which then speaks to the core as that
    plugin. The plugin reports what it managed through its answer: nothing is written outside its
    sandbox for the test to read."""
    target = short_dir / "runner.sock"
    install(short_dir, "hijack", HIJACK)

    async def scenario() -> list[str]:
        server = server_in(short_dir)
        await server.start()
        try:
            before = target.stat().st_ino
            attacker = await session(server, "rpc-demo", "hijack:make")
            tried = await search(attacker, str(target))
            # the socket the core connects to is still the companion's
            assert target.stat().st_ino == before, f"a plugin replaced the socket: {tried}"
            victim = await session(server, "rpc-demo-2", "rpc_plugin:make_other", timeout=3.0)
            assert "rpc-demo-2" in server.pids()
            await victim.close()
            await attacker.close()
            return tried
        finally:
            await server.close()

    tried = run(scenario)
    assert tried == ["take_over:PermissionError"], tried


CHMOD = """
import os
from rpc_plugin import Plugin, _ref

class Lock(Plugin):
    async def search(self, f, q, filters, cursor, *, sub=None):
        from shelfhold_sdk.models import Page
        target = q
        out = []
        for name, path, mode in (("socket", target, 0o600),
                                 ("folder", os.path.dirname(target), 0o700)):
            try:
                os.chmod(path, mode)
                out.append(f"{name}:ok")
            except OSError as exc:
                out.append(f"{name}:{type(exc).__name__}")
        return Page(items=[_ref(x) for x in out])

def make():
    return Lock()
"""


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_a_plugin_cannot_lock_the_core_out_of_the_runner_socket(short_dir: Path) -> None:
    """Landlock handles no change of mode: the plugin owns `/plugin-rpc` and `runner.sock` (the
    companion's uid) and makes them `0700` and `0600`. The core (uid 1000) can no longer reach the
    socket, the companion's own healthcheck (uid 1001) still can: every isolated plugin is down
    while the container reports healthy, and the folder's mode stays on the volume after a
    restart."""
    target = short_dir / "runner.sock"
    install(short_dir, "lock", CHMOD)
    folder_mode = stat.S_IMODE(short_dir.stat().st_mode)

    async def scenario() -> tuple[list[str], int, int]:
        server = server_in(short_dir)
        await server.start()
        try:
            s = await session(server, "rpc-demo", "lock:make")
            tried = await search(s, str(target))
            await s.close()
            modes = (
                stat.S_IMODE(target.stat().st_mode),
                stat.S_IMODE(short_dir.stat().st_mode),
            )
            return tried, *modes
        finally:
            with contextlib.suppress(OSError):
                short_dir.chmod(folder_mode)
            await server.close()

    tried, socket_mode, now_folder = run(scenario)
    assert socket_mode == 0o666, f"a plugin changed the socket's mode: {tried}"
    assert now_folder == folder_mode, f"a plugin changed the socket folder's mode: {tried}"


ESCAPE = """
import os, socket, time
from rpc_plugin import Plugin, _ref

class Escape(Plugin):
    async def search(self, f, q, filters, cursor, *, sub=None):
        from shelfhold_sdk.models import Page
        token, mode = q.split("|")
        if os.fork() == 0:
            os.setsid()
            if os.fork() == 0:
                os.closerange(0, 4096)  # nothing of the plugin's: only what it makes now
                held = socket.socket(socket.AF_UNIX)
                held.bind("\\0" + token)  # abstract: no file, nothing Landlock looks at
                held.listen(8)
                held.settimeout(0.02)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        conn, _ = held.accept()
                        conn.settimeout(0.5)
                        if conn.recv(1) == b"q":
                            os._exit(0)
                        conn.close()
                    except OSError:
                        pass
                    if mode == "hop":
                        try:
                            if os.fork() != 0:
                                os._exit(0)  # the parent goes: a new pid, adopted by the subreaper
                        except OSError:
                            pass
                os._exit(0)
            os._exit(0)
        os.wait()
        return Page(items=[_ref("started")])

def make():
    return Escape()
"""


def _alive(token: str) -> bool:
    """The escaped process answers on its abstract socket while it lives (nothing is written)."""
    probe = socket.socket(socket.AF_UNIX)
    probe.settimeout(1.0)
    try:
        probe.connect("\0" + token)
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _stop(token: str) -> None:
    for _ in range(50):
        probe = socket.socket(socket.AF_UNIX)
        probe.settimeout(1.0)
        try:
            probe.connect("\0" + token)
            probe.sendall(b"q")
        except OSError:
            return
        finally:
            probe.close()
        time.sleep(0.1)


def _reap_ours() -> None:
    """The hops this process adopted as the subreaper are zombies until it waits for them."""
    with contextlib.suppress(ChildProcessError):
        while os.waitpid(-1, os.WNOHANG)[0] > 0:
            pass


def _adopted_zombies(keep: set[int]) -> set[int]:
    """This process's children that are zombies (the hops it adopted), from the kernel's list."""
    children: set[int] = set()
    for task in os.listdir("/proc/self/task"):
        with contextlib.suppress(OSError):
            listed = Path(f"/proc/self/task/{task}/children").read_text()
            children |= {int(p) for p in listed.split()}
    zombies = set()
    for pid in children - keep:
        with contextlib.suppress(OSError):
            if Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] == "Z":
                zombies.add(pid)
    return zombies


async def _reaper(server: RunnerServer) -> None:
    """Collects the hops' zombies as they come, so that a test machine keeps its pids. This helps
    the companion (its own sweep collects them only when a connection ends), never the plugin."""
    while True:
        for pid in _adopted_zombies(set(server.pids().values())):
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        await asyncio.sleep(0.05)


def _escape(short_dir: Path, mode: str) -> None:
    token = f"shelfhold-review-{mode}-{os.getpid()}-{time.monotonic_ns()}"
    install(short_dir, "escape", ESCAPE)
    survived: bool | None = None

    async def scenario() -> None:
        nonlocal survived
        server = server_in(short_dir)
        await server.start()
        reaper = asyncio.ensure_future(_reaper(server))
        try:
            s = await session(server, "rpc-demo", "escape:make")
            assert await search(s, f"{token}|{mode}") == ["started"]
            for _ in range(40):
                if _alive(token):
                    break
                await asyncio.sleep(0.05)
            assert _alive(token), "the escaped process did not start"
            await s.close()
            for _ in range(100):
                if "rpc-demo" not in server.pids():
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(1.0)
            survived = await asyncio.to_thread(_alive, token)
        finally:
            await asyncio.to_thread(_stop, token)
            await server.close()
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper

    try:
        run(scenario)
        assert survived is False, f"{mode}: a process the plugin started outlived its connection"
    finally:
        _stop(token)
        _reap_ours()


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_a_plugin_process_does_not_outlive_its_connection_by_leaving_its_session(
    short_dir: Path,
) -> None:
    """`killpg` kills the plugin's process group: a grandchild that calls `setsid()` is in another
    group. It answers on an abstract socket while it lives: nothing is written."""
    _escape(short_dir, "stay")


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_a_plugin_process_does_not_outlive_its_connection_by_changing_its_pid(
    short_dir: Path,
) -> None:
    """The sweep reads `/proc`, then kills what it read, five times over: a process that forks and
    lets its parent exit every 20 ms is never the pid that was read."""
    _escape(short_dir, "hop")


@pytest.mark.req("EF-SRC-016", "EF-SRC-019", "ENF-SEC-009")
def test_a_plugin_cannot_read_the_companion_process(short_dir: Path) -> None:
    """The companion stays dumpable: its plugins (same uid) read `/proc/<companion>/environ`, and
    with `ptrace` (allowed by Docker's default profile, and by Yama at scope 0) its memory, which
    carries every other plugin's connection to the core."""
    install(
        short_dir,
        "peek",
        """
        import os
        from rpc_plugin import Plugin, _ref

        class Peek(Plugin):
            async def search(self, f, q, filters, cursor, *, sub=None):
                from shelfhold_sdk.models import Page
                try:
                    seen = open(f"/proc/{os.getppid()}/environ", "rb").read()
                except OSError as exc:
                    return Page(items=[_ref(f"refused:{type(exc).__name__}")])
                return Page(items=[_ref(f"read:{seen.count(bytes(1))} variables")])

        def make():
            return Peek()
        """,
    )

    async def scenario() -> list[str]:
        server = server_in(short_dir)
        await server.start()
        try:
            s = await session(server, "rpc-demo", "peek:make")
            try:
                return await search(s, "x")
            finally:
                await s.close()
        finally:
            await server.close()

    seen = run(scenario)
    assert seen[0].startswith("refused:"), f"the plugin read its companion's process: {seen}"
