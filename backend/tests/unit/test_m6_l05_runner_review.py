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
import tempfile
import textwrap
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
        RunnerConfig(socket_path=root / "runner.sock", plugins_root=root / "plugins"),
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


@pytest.mark.req("EF-SRC-016", "EF-SRC-019", "ENF-SEC-009")
def test_a_plugin_cannot_take_the_runner_socket_over(short_dir: Path) -> None:
    """A plugin unlinks `runner.sock` (its folder is its uid's) and binds its own: the next plugin
    the core asks for is greeted by the first plugin, which then speaks to the core as that plugin."""
    target = short_dir / "runner.sock"
    install(
        short_dir,
        "hijack",
        f"""
        import os, socket
        from rpc_plugin import make as _make

        def make():
            os.unlink({str(target)!r})
            held = socket.socket(socket.AF_UNIX)
            held.bind({str(target)!r})
            held.listen(8)
            globals()["_held"] = held  # a listener the core will connect to, in place of the companion
            return _make()
        """,
    )

    async def scenario() -> None:
        server = server_in(short_dir)
        await server.start()
        try:
            before = target.stat().st_ino
            attacker = await session(server, "rpc-demo", "hijack:make")
            assert await search(attacker, "a") == ["echo:a"]
            # the socket the core connects to is still the companion's
            assert target.stat().st_ino == before, "a plugin replaced the companion's socket"
            victim = await session(server, "rpc-demo-2", "rpc_plugin:make_other", timeout=3.0)
            assert "rpc-demo-2" in server.pids()
            await victim.close()
            await attacker.close()
        finally:
            await server.close()

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_a_plugin_process_does_not_outlive_its_connection_by_leaving_its_session(
    short_dir: Path,
) -> None:
    """`killpg` kills the plugin's process group: a grandchild that calls `setsid()` is in another
    group, and lives on after the core has hung up (and after the plugin is disabled)."""
    pidfile = short_dir / "escaped.pid"
    install(
        short_dir,
        "escape",
        f"""
        import os, time
        from rpc_plugin import make as _make

        def make():
            if os.fork() == 0:
                os.setsid()
                if os.fork() == 0:
                    os.closerange(0, 3)
                    with open({str(pidfile)!r}, "w") as out:
                        out.write(str(os.getpid()))
                    time.sleep(120)
                    os._exit(0)
                os._exit(0)
            os.wait()
            return _make()
        """,
    )
    escaped: int | None = None

    async def scenario() -> None:
        nonlocal escaped
        server = server_in(short_dir)
        await server.start()
        try:
            s = await session(server, "rpc-demo", "escape:make")
            assert await search(s, "a") == ["echo:a"]
            for _ in range(100):
                if pidfile.exists() and pidfile.read_text():
                    break
                await asyncio.sleep(0.05)
            escaped = int(pidfile.read_text())
            await s.close()
            for _ in range(100):
                if "rpc-demo" not in server.pids():
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)
        finally:
            await server.close()

    try:
        run(scenario)
        assert escaped is not None
        with pytest.raises(ProcessLookupError):
            os.kill(escaped, 0)  # the plugin's connection is gone: so is every process it started
    finally:
        if escaped is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(escaped, signal.SIGKILL)


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
