"""M6 L05 (EF-SRC-016, EF-SRC-019, EF-USR-024, ENF-SEC-009, D-P1): the plugin runner, the companion
that starts the isolated plugins' processes.

The companion has another uid than the core, sees neither `/data` nor `/store` nor `/library` nor
the CLI socket, has no network and a read-only root; it checks all of that when it starts and
refuses to start when one of them is false. The core connects to its socket (never the reverse);
the companion starts one subprocess per plugin, with a closed environment. The tests below play
the companion in this process, with real subprocesses for the plugins, and describe the sandbox by
observations a test makes up (the compose of the e2e stack plays the real one).
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import os
import re
import secrets
import shutil
import signal
import socket
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.plugin_host.adapters.rpc.runner_socket import (
    RunnerRefused,
    RunnerUnavailable,
    connect_runner,
)
from shelfhold.plugin_host.rpc import codec
from shelfhold.plugin_host.rpc.host_side import RpcSession
from shelfhold.plugin_host.rpc.protocol import Channel, RpcLimits
from shelfhold.plugin_host.runner.selfcheck import (
    FORBIDDEN_PATHS,
    Observation,
    SelfCheckFailed,
    interfaces_of,
    observe,
    probe_outbound,
    problems_of,
    self_check,
)
from shelfhold.plugin_host.runner.server import RunnerConfig, RunnerServer, serve

FAKES = Path(__file__).resolve().parent.parent / "fakes"
SRC = Path(__file__).resolve().parents[2] / "src/shelfhold"
CORE_UID, RUNNER_UID = 1000, 1001

CLEAN = Observation(
    uid=RUNNER_UID,
    visible_paths=(),
    interfaces=("lo",),
    outbound_possible=False,
    root_read_only=True,
)


def run[T](make: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(asyncio.wait_for(make(), 90))


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A short path: a unix socket's address is at most 107 bytes, which pytest's paths exceed."""
    path = Path(tempfile.mkdtemp(prefix="rn", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_plugins(root: Path, *modules: str) -> Path:
    site = root / "plugins" / "site-packages"
    site.mkdir(parents=True)
    for module in modules or ("rpc_plugin",):
        shutil.copy(FAKES / f"{module}.py", site / f"{module}.py")
    return root / "plugins"


def make_server(
    root: Path,
    *,
    own_uid: int = RUNNER_UID,
    peer_uid: int | None = CORE_UID,
    observation: Observation = CLEAN,
    **config: Any,
) -> RunnerServer:
    return RunnerServer(
        RunnerConfig(socket_path=root / "runner.sock", plugins_root=root / "plugins", **config),
        own_uid=own_uid,
        peer_uid=(lambda _sock: peer_uid) if peer_uid is not None else None,
        observe=lambda: observation,
    )


@contextlib.asynccontextmanager
async def running(server: RunnerServer) -> AsyncIterator[RunnerServer]:
    await server.start()
    try:
        yield server
    finally:
        await server.close()


async def open_session(
    server: RunnerServer,
    key: str = "rpc-demo",
    entry: str = "rpc_plugin:make",
    *,
    core_uid: int = CORE_UID,
    wrap: Callable[[Channel], Channel] | None = None,
) -> RpcSession:
    limits = RpcLimits()
    channel = await connect_runner(
        server.config.socket_path, key=key, entry=entry, limits=limits, core_uid=core_uid
    )
    return await RpcSession.open(wrap(channel) if wrap else channel, key=key, limits=limits)


def search(session: RpcSession, q: str) -> Awaitable[Any]:
    return session.call("source.search", {"q": q, "filters": {}, "cursor": None}, fetcher=None)


def titles(page: Any) -> list[str]:
    return [item.title for item in page.items]


def pid_of(server: RunnerServer, key: str) -> int:
    return server.pids()[key]


# -- the self-check at start ---------------------------------------------------------------------


@pytest.mark.req("EF-SRC-016", "EF-USR-024", "ENF-SEC-009")
@pytest.mark.parametrize("name", ["data", "store", "library", "data/shelfhold.sock"])
def test_plugin_runner_refuses_to_start_when_it_sees_data(name: str, tmp_path: Path) -> None:
    # the four names the card lists, as seen from the companion: each one, alone, stops it
    paths = {n: str(tmp_path / n) for n in ("data", "store", "library", "data/shelfhold.sock")}
    assert set(FORBIDDEN_PATHS) == {"/data", "/store", "/library", "/data/shelfhold.sock"}
    (tmp_path / "data").mkdir()
    (tmp_path / "store").mkdir()
    (tmp_path / "library").mkdir()
    for n in ("data", "store", "library"):
        (tmp_path / n).rmdir()
    assert not observe(paths=tuple(paths.values())).visible_paths  # nothing there: nothing seen
    target = tmp_path / name
    if name.endswith(".sock"):
        (tmp_path / "data").mkdir()
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(target))
        listener.listen()
    else:
        target.mkdir(parents=True)
        listener = None
    try:
        seen = observe(paths=tuple(paths.values()))
        assert str(target) in seen.visible_paths
        with pytest.raises(SelfCheckFailed) as refused:
            self_check(replace(CLEAN, visible_paths=seen.visible_paths), core_uid=CORE_UID)
        assert str(target) in "\n".join(refused.value.problems)
    finally:
        if listener is not None:
            listener.close()


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_refuses_to_start_when_it_sees_data_and_binds_nothing(
    short_dir: Path,
) -> None:
    server = make_server(short_dir, observation=replace(CLEAN, visible_paths=("/data",)))
    with pytest.raises(SelfCheckFailed):
        run(server.start)
    assert not server.config.socket_path.exists()  # it never listened


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_sees_a_path_it_cannot_read_as_absent(tmp_path: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("root reads everything")
    locked = tmp_path / "data"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert not observe(paths=(str(locked),)).visible_paths  # present but unreadable: fine
    finally:
        locked.chmod(0o700)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_refuses_a_writable_root_and_a_stray_interface_and_root_user() -> None:
    assert problems_of(CLEAN, core_uid=CORE_UID) == []
    assert any("read-only" in p for p in problems_of(replace(CLEAN, root_read_only=False)))
    assert any("root" in p for p in problems_of(replace(CLEAN, uid=0), core_uid=CORE_UID))
    with pytest.raises(SelfCheckFailed) as refused:
        self_check(replace(CLEAN, root_read_only=False, interfaces=("lo", "eth0")))
    assert len(refused.value.problems) == 2  # it says everything that is wrong, not the first


@pytest.mark.req("EF-SRC-016", "EF-USR-024", "ENF-SEC-009")
def test_plugin_subprocess_uid_distinct_from_core() -> None:
    assert problems_of(CLEAN, core_uid=CORE_UID) == []
    shared = problems_of(CLEAN, core_uid=RUNNER_UID)
    assert shared and any("uid" in p for p in shared)  # the core's uid, read in the handshake


@pytest.mark.req("EF-SRC-016", "EF-USR-024", "ENF-SEC-009")
def test_plugin_subprocess_uid_distinct_from_core_in_the_handshake(short_dir: Path) -> None:
    make_plugins(short_dir)

    async def scenario() -> None:
        # the core and the companion share a uid: refused, whatever the core claims
        same = make_server(short_dir, own_uid=CORE_UID, peer_uid=CORE_UID)
        async with running(same):
            with pytest.raises(RunnerRefused, match="uid"):
                await open_session(same)
        # a core that claims another uid than the one the kernel reports for it: refused
        liar = make_server(short_dir, peer_uid=4242)
        async with running(liar):
            with pytest.raises(RunnerRefused, match="uid"):
                await open_session(liar, core_uid=CORE_UID)
        # distinct uids, and the claim is the kernel's: served, and the plugin answers
        honest = make_server(short_dir)
        async with running(honest):
            session = await open_session(honest)
            try:
                assert titles(await search(session, "hello")) == ["echo:hello"]
            finally:
                await session.close()

    run(scenario)


@pytest.mark.req("EF-SRC-016", "EF-USR-024", "ENF-SEC-009")
def test_plugin_runner_reads_the_core_uid_from_the_kernel_by_default(short_dir: Path) -> None:
    # no injected `peer_uid`: SO_PEERCRED. Same process, same uid: the companion refuses it.
    make_plugins(short_dir)

    async def scenario() -> None:
        server = RunnerServer(
            RunnerConfig(socket_path=short_dir / "runner.sock", plugins_root=short_dir / "plugins"),
            own_uid=os.getuid() + 7,
            observe=lambda: replace(CLEAN, uid=os.getuid() + 1),
        )
        async with running(server):
            with pytest.raises(RunnerRefused, match="uid"):
                await open_session(
                    server, core_uid=os.getuid() + 1
                )  # the claim is not the kernel's
            session = await open_session(server, core_uid=os.getuid())
            await session.close()

    run(scenario)


# -- the network --------------------------------------------------------------------------------

PROC_NET_DEV = (
    "Inter-|   Receive   |  Transmit\n"
    " face |bytes packets errs|bytes packets errs\n"
    "    lo:  0  0  0  0  0\n"
    "  eth0:  1234  10  0  999  8\n"
)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_subprocess_has_no_network(tmp_path: Path) -> None:
    listing = tmp_path / "dev"
    listing.write_text(PROC_NET_DEV, encoding="utf-8")
    assert interfaces_of(listing) == ("lo", "eth0")
    assert any("eth0" in p for p in problems_of(replace(CLEAN, interfaces=("lo", "eth0"))))
    # an outbound connection that goes through is a network, whatever the interfaces say
    outbound = problems_of(replace(CLEAN, outbound_possible=True))
    assert any("outbound" in p for p in outbound)
    assert problems_of(replace(CLEAN, interfaces=("lo",))) == []

    async def probe() -> tuple[bool, bool]:
        served = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        (port,) = {s.getsockname()[1] for s in served.sockets}
        loop = asyncio.get_running_loop()
        open_ = await loop.run_in_executor(None, probe_outbound, ("127.0.0.1", port))
        served.close()
        await served.wait_closed()
        closed = await loop.run_in_executor(None, probe_outbound, ("127.0.0.1", port))
        return open_, closed

    reached, refused = run(probe)
    assert reached is True  # something answered: a network is there
    assert refused is False  # refused (or unreachable): none


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_subprocess_has_no_network_observed_for_real(short_dir: Path) -> None:
    # the real observation has the shape the check reads, whatever machine this runs on
    seen = observe(paths=(str(short_dir / "nothing"),), probe=("127.0.0.1", 9))
    assert seen.uid == os.getuid()
    assert "lo" in seen.interfaces
    assert seen.visible_paths == ()
    assert isinstance(seen.root_read_only, bool)


# -- one subprocess per plugin, closed environment ----------------------------------------------


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_one_subprocess_per_plugin_and_it_ends_with_the_connection(
    short_dir: Path,
) -> None:
    make_plugins(short_dir, "rpc_plugin")

    async def scenario() -> None:
        server = make_server(short_dir)
        async with running(server):
            one = await open_session(server, "rpc-demo", "rpc_plugin:make")
            other = await open_session(server, "rpc-demo-2", "rpc_plugin:make_other")
            first, second = pid_of(server, "rpc-demo"), pid_of(server, "rpc-demo-2")
            assert first != second and os.getpid() not in (first, second)
            assert titles(await search(one, "a")) == ["echo:a"]
            assert titles(await search(other, "b")) == ["echo:b"]
            # a plugin runs once: a second connection for the same key is refused
            with pytest.raises(RunnerRefused, match="already"):
                await open_session(server, "rpc-demo", "rpc_plugin:make")
            await one.close()
            for _ in range(100):  # the process goes when its connection does
                if "rpc-demo" not in server.pids():
                    break
                await asyncio.sleep(0.05)
            assert "rpc-demo" not in server.pids()
            with pytest.raises(ProcessLookupError):
                os.kill(first, 0)
            again = await open_session(server, "rpc-demo", "rpc_plugin:make")  # and may come back
            await again.close()
            await other.close()

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_subprocess_is_closed_umask_077_no_core_dump(short_dir: Path) -> None:
    make_plugins(short_dir)

    async def scenario() -> None:
        server = make_server(short_dir)
        async with running(server):
            session = await open_session(server)
            try:
                pid = pid_of(server, "rpc-demo")
                status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
                assert re.search(r"^Umask:\s+0?077$", status, re.MULTILINE)
                limits = Path(f"/proc/{pid}/limits").read_text(encoding="utf-8")
                core = next(ln for ln in limits.splitlines() if ln.startswith("Max core file size"))
                assert core.split()[-3:-1] == ["0", "0"]
                # a session of its own, and none of the companion's descriptors
                assert os.getsid(pid) == pid
                fds = {int(n) for n in os.listdir(f"/proc/{pid}/fd")}
                assert fds <= {0, 1, 2, 3, 4, 5, 6, 7, 8, 9}  # std streams and the loop's own
                links = [os.readlink(f"/proc/{pid}/fd/{n}") for n in fds]
                assert not any(str(short_dir) in link for link in links)  # not the listening socket
            finally:
                await session.close()

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_subprocess_environment_carries_no_secret(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_plugins(short_dir)
    token = secrets.token_urlsafe(24)
    for name in (
        "SHELFHOLD_SETUP_TOKEN",
        "SHELFHOLD_SECRET_KEY_FILE",
        "SHELFHOLD_DATA_DIR",
        "API_TOKEN",
        "DB_PASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, token)
    monkeypatch.setenv("PATH", os.environ["PATH"])

    async def scenario() -> None:
        server = make_server(short_dir)
        async with running(server):
            session = await open_session(server)
            try:
                pid = pid_of(server, "rpc-demo")
                environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                names = {e.split(b"=", 1)[0].decode() for e in environ if e}
                assert token.encode() not in b"\0".join(environ)
                assert not [n for n in names if n.startswith("SHELFHOLD_")]
                assert not [n for n in names if re.search("SECRET|TOKEN|PASSWORD|KEY", n)]
                assert "PYTHONPATH" not in names  # nor anything that changes what it imports
                # and what the plugin sees of its own environment is the same
                seen = titles(await search(session, "env"))[0].split("|")
                assert not [n for n in seen if n.startswith("SHELFHOLD_")]
                assert not [n for n in seen if re.search("SECRET|TOKEN|PASSWORD|KEY", n)]
            finally:
                await session.close()

    run(scenario)


class Tap:
    """A channel that keeps every byte that crosses it, in both directions."""

    def __init__(self, inner: Channel) -> None:
        self.sent = bytearray()
        self.received = bytearray()
        inner_writer = inner.writer
        outer = self

        class Writer:
            def write(self, data: bytes) -> None:
                outer.sent.extend(data)
                inner_writer.write(data)

            async def drain(self) -> None:
                await inner_writer.drain()

            def close(self) -> None:
                inner_writer.close()

        class Reader(asyncio.StreamReader):
            pass

        self.channel = Channel(inner.reader, Writer(), inner.close, inner.exit_reason)
        original = inner.reader.read

        async def read(n: int = -1) -> bytes:
            data = await original(n)
            outer.received.extend(data)
            return data

        inner.reader.read = read  # type: ignore[method-assign]


@pytest.mark.req("EF-SRC-016", "EF-SRC-019", "ENF-SEC-009")
def test_no_secret_passed_to_plugin(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolated mode: no message, no environment variable, no argument carries a value of the
    keyring. The host holds the secret and a Fetcher on its side injects it: the plugin has none."""
    make_plugins(short_dir)
    secret = secrets.token_urlsafe(24)
    register_secret_value(secret)
    monkeypatch.setenv("SHELFHOLD_SETUP_TOKEN", secret)
    taps: list[Tap] = []

    def tapped(channel: Channel) -> Channel:
        taps.append(Tap(channel))
        return taps[-1].channel

    async def scenario() -> None:
        server = make_server(short_dir)
        async with running(server):
            session = await open_session(server, wrap=tapped)
            try:
                assert titles(await search(session, "hello")) == ["echo:hello"]
                from shelfhold.plugin_host.rpc.protocol import SecretLeak

                with pytest.raises(SecretLeak):  # a message that would carry it is not sent
                    await search(session, f"find {secret}")
                pid = pid_of(server, "rpc-demo")
                argv = Path(f"/proc/{pid}/cmdline").read_bytes()
                environ = Path(f"/proc/{pid}/environ").read_bytes()
                assert secret.encode() not in argv  # arguments
                assert secret.encode() not in environ  # environment
                assert secret.encode() not in bytes(taps[0].sent)  # messages, to the plugin
                assert secret.encode() not in bytes(taps[0].received)  # and from it
            finally:
                await session.close()

    run(scenario)


# -- what the core may and may not do on the runner's volume --------------------------------------


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_core_never_listens_on_the_runner_volume(short_dir: Path) -> None:
    # the core connects; it creates nothing where the companion listens, and when no companion
    # is there it fails, leaving the directory as it found it
    volume = short_dir / "plugin-rpc"
    volume.mkdir()

    async def scenario() -> None:
        with pytest.raises(RunnerUnavailable):
            await connect_runner(
                volume / "runner.sock", key="a", entry="a:b", limits=RpcLimits(), core_uid=CORE_UID
            )
        assert list(volume.iterdir()) == []
        decoy = volume / "runner.sock"
        decoy.write_text("not a socket", encoding="utf-8")  # a file where the socket should be
        with pytest.raises(RunnerUnavailable):
            await connect_runner(decoy, key="a", entry="a:b", limits=RpcLimits(), core_uid=CORE_UID)
        assert decoy.read_text(encoding="utf-8") == "not a socket"

    run(scenario)

    # and by construction: the only code that binds a unix socket is the CLI socket's server, on
    # the data directory, and the companion's, on the volume the companion owns
    listeners: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Attribute):
                continue
            serves = node.attr in {"start_unix_server", "create_unix_server"}
            binds = node.attr in {"bind", "listen"} and "AF_UNIX" in text
            if serves or binds:
                listeners.add(path.relative_to(SRC).as_posix())
    allowed = {
        "infrastructure/local_socket/server.py",  # the CLI socket, in the data directory
        "plugin_host/runner/server.py",  # the companion's
    }
    assert listeners == allowed, listeners
    core_side = (SRC / "plugin_host/adapters/rpc/runner_socket.py").read_text(encoding="utf-8")
    assert "bind(" not in core_side and "start_unix_server" not in core_side
    assert "local_socket" not in core_side  # nor the CLI's socket, nor its code
    cli_socket = (SRC / "infrastructure/local_socket/server.py").read_text(encoding="utf-8")
    assert "plugin" not in cli_socket.lower().replace("plugins", "")  # no shared methods


# -- what the companion refuses to start ----------------------------------------------------------


@pytest.mark.req("EF-SRC-016", "EF-SRC-019", "ENF-SEC-009")
def test_plugin_runner_starts_only_what_the_volume_holds(short_dir: Path) -> None:
    make_plugins(short_dir)
    outside = short_dir / "elsewhere"
    outside.mkdir()
    (outside / "stray_plugin.py").write_text("def make():\n    raise SystemExit(0)\n")

    async def scenario() -> None:
        server = make_server(short_dir)
        async with running(server):
            for key, entry, why in (
                ("rpc-demo", "stray_plugin:make", "not installed"),  # not on the volume
                ("rpc-demo", "os:system", "not installed"),  # the standard library is no plugin
                ("rpc-demo", "shelfhold.main:app_from_env", "not installed"),  # nor is the core
                ("rpc-demo", "../x:make", "entry"),
                ("rpc-demo", "rpc_plugin", "entry"),
                ("rpc-demo", "rpc_plugin:make --path /data", "entry"),
                ("../etc", "rpc_plugin:make", "key"),
                ("A" * 80, "rpc_plugin:make", "key"),
                ("", "rpc_plugin:make", "key"),
            ):
                with pytest.raises(RunnerRefused, match=why):
                    await open_session(server, key, entry)
            assert server.pids() == {}  # nothing was started

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_refuses_garbage_and_oversized_hellos(short_dir: Path) -> None:
    make_plugins(short_dir)

    async def raw(server: RunnerServer, payload: bytes) -> bytes:
        reader, writer = await asyncio.open_unix_connection(str(server.config.socket_path))
        writer.write(payload)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(65536), 10)
        writer.close()
        return data

    async def scenario() -> None:
        server = make_server(short_dir)
        async with running(server):
            # a length that announces a megabyte: the hello is small, refused before any read
            assert b"refused" in await raw(server, (1 << 20).to_bytes(4, "big"))
            assert b"refused" in await raw(server, b"\x00\x00\x00\x05[1,2]")  # not a request
            body = b'{"jsonrpc":"2.0","id":1,"method":"fetcher.get","params":{}}'
            assert b"refused" in await raw(server, len(body).to_bytes(4, "big") + body)
            # a client that leaves without a word costs it nothing
            _, writer = await asyncio.open_unix_connection(str(server.config.socket_path))
            writer.close()
            await asyncio.sleep(0.1)
            session = await open_session(server)  # and it still serves
            await session.close()

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_refuses_a_session_when_the_sandbox_has_changed(short_dir: Path) -> None:
    """The self-check is the start's, and each connection's: a companion that came to see `/data`
    (a volume added by mistake) serves nothing more."""
    make_plugins(short_dir)
    state = {"seen": CLEAN}
    server = RunnerServer(
        RunnerConfig(socket_path=short_dir / "runner.sock", plugins_root=short_dir / "plugins"),
        own_uid=RUNNER_UID,
        peer_uid=lambda _s: CORE_UID,
        observe=lambda: state["seen"],
    )

    async def scenario() -> None:
        async with running(server):
            session = await open_session(server)
            await session.close()
            state["seen"] = replace(CLEAN, visible_paths=("/data",))
            with pytest.raises(RunnerRefused, match="/data"):
                await open_session(server)

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_limits_the_number_of_plugins(short_dir: Path) -> None:
    make_plugins(short_dir)

    async def scenario() -> None:
        server = make_server(short_dir, max_plugins=1)
        async with running(server):
            one = await open_session(server, "rpc-demo", "rpc_plugin:make")
            with pytest.raises(RunnerRefused, match="too many"):
                await open_session(server, "rpc-demo-2", "rpc_plugin:make_other")
            await one.close()

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_clamps_the_limits_the_core_asks_for(short_dir: Path) -> None:
    # the core may ask for less, never more than the companion's own ceiling
    make_plugins(short_dir)

    async def scenario() -> None:
        server = make_server(short_dir, limits=RpcLimits(memory_bytes=300 * 1024 * 1024))
        async with running(server):
            ask = RpcLimits(memory_bytes=4 * 1024**3, cpu_seconds=10**6)
            channel = await connect_runner(
                server.config.socket_path,
                key="rpc-demo",
                entry="rpc_plugin:make",
                limits=ask,
                core_uid=CORE_UID,
            )
            session = await RpcSession.open(channel, key="rpc-demo", limits=ask)
            try:
                pid = pid_of(server, "rpc-demo")
                limits = Path(f"/proc/{pid}/limits").read_text(encoding="utf-8")
                space = next(ln for ln in limits.splitlines() if ln.startswith("Max address space"))
                assert int(space.split()[3]) == 300 * 1024 * 1024
            finally:
                await session.close()

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_that_crashes_marks_the_session_and_not_the_companion(short_dir: Path) -> None:
    make_plugins(short_dir)

    async def scenario() -> None:
        server = make_server(short_dir)
        async with running(server):
            session = await open_session(server)
            from shelfhold.plugin_host.rpc.protocol import PluginFailed

            with pytest.raises(PluginFailed):
                await search(session, "crash")
            assert session.state == "error"
            for _ in range(100):
                if "rpc-demo" not in server.pids():
                    break
                await asyncio.sleep(0.05)
            assert server.pids() == {}  # nothing is left of it
            again = await open_session(server)  # the companion goes on
            assert titles(await search(again, "ok")) == ["echo:ok"]
            await again.close()

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_close_stops_every_plugin_and_removes_its_socket(short_dir: Path) -> None:
    make_plugins(short_dir)

    async def scenario() -> None:
        server = make_server(short_dir)
        await server.start()
        mode = server.config.socket_path.stat().st_mode & 0o777
        assert mode == 0o666  # the core's uid is another: a socket it can connect to
        session = await open_session(server)
        pid = pid_of(server, "rpc-demo")
        await server.close()
        assert not server.config.socket_path.exists()
        with pytest.raises(ProcessLookupError):
            for _ in range(100):
                os.kill(pid, 0)
                await asyncio.sleep(0.05)
        await session.close()

    run(scenario)


# -- the rest of the companion's edges ------------------------------------------------------------


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_serves_until_sigterm_and_leaves_nothing_behind(short_dir: Path) -> None:
    make_plugins(short_dir)
    server = make_server(short_dir)
    said: list[str] = []

    async def scenario() -> None:
        async def ready() -> None:
            said.append("ready")
            session = await open_session(server)
            assert titles(await search(session, "up")) == ["echo:up"]
            await session.close()
            os.kill(os.getpid(), signal.SIGTERM)  # the container's stop: the loop's handler

        await serve(server.config, ready=ready, server=server)

    run(scenario)
    assert said == ["ready"]
    assert not server.config.socket_path.exists() and server.pids() == {}


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_refuses_start_without_binding_through_serve(short_dir: Path) -> None:
    server = make_server(short_dir, observation=replace(CLEAN, interfaces=("lo", "eth0")))
    with pytest.raises(SelfCheckFailed, match="eth0"):
        run(lambda: serve(server.config, server=server))
    assert not server.config.socket_path.exists()


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_replaces_a_stale_socket_and_never_a_stray_file(short_dir: Path) -> None:
    make_plugins(short_dir)

    async def scenario() -> None:
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(short_dir / "runner.sock"))
        stale.close()  # a companion that died: its socket file is still there
        server = make_server(short_dir)
        async with running(server):
            session = await open_session(server)
            await session.close()
        (short_dir / "runner.sock").write_text("mine", encoding="utf-8")
        with pytest.raises(OSError):  # a file that is not a socket is not the companion's to remove
            await make_server(short_dir).start()
        assert (short_dir / "runner.sock").read_text(encoding="utf-8") == "mine"

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_says_so_when_the_process_cannot_start(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_plugins(short_dir)

    async def broken(*_a: Any, **_k: Any) -> Channel:
        raise OSError("no fork")

    monkeypatch.setattr("shelfhold.plugin_host.runner.server.spawn_stdio", broken)

    async def scenario() -> None:
        server = make_server(short_dir)
        async with running(server):
            with pytest.raises(RunnerRefused, match="could not be started"):
                await open_session(server)
            assert server.pids() == {}
            monkeypatch.undo()
            session = await open_session(server)  # and the key was given back
            await session.close()

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_without_a_plugins_folder_holds_no_plugin(short_dir: Path) -> None:
    async def scenario() -> None:
        server = make_server(short_dir)  # nothing was ever installed: no site-packages
        async with running(server):
            with pytest.raises(RunnerRefused, match="not installed"):
                await open_session(server)

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_the_core_end_names_what_it_cannot_do_when_the_companion_misbehaves(
    short_dir: Path,
) -> None:
    """A companion that hangs up, speaks nonsense or answers with something else than a result is
    `RunnerUnavailable`, never an exception of another kind and never a started plugin."""
    path = short_dir / "odd.sock"

    async def scenario() -> None:
        async def odd(answer: bytes | None) -> None:
            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                await codec.read_frame(reader)
                if answer is not None:
                    writer.write(answer)
                    await writer.drain()
                writer.close()

            server = await asyncio.start_unix_server(handle, path=str(path))
            try:
                with pytest.raises(RunnerUnavailable):
                    await connect_runner(
                        path, key="a", entry="a:b", limits=RpcLimits(), core_uid=CORE_UID
                    )
            finally:
                server.close()
                await server.wait_closed()

        await odd(None)  # hangs up without a word
        await odd(b"\x00\x00\x00\x03abc")  # a frame that is not JSON
        await odd(codec.encode_frame({"jsonrpc": "2.0", "id": 1, "result": 5}))  # not an ack

    run(scenario)


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_the_observation_reads_what_is_there_and_says_so_when_it_cannot(tmp_path: Path) -> None:
    gone = tmp_path / "net"
    assert observe(paths=(), proc_net_dev=gone).interfaces == ("?",)  # unreadable: not vouched for
    assert problems_of(observe(paths=(), proc_net_dev=gone))
    seen = self_check(replace(CLEAN))  # a clean observation is returned as it is
    assert seen == CLEAN


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_the_cli_command_serves_what_it_was_given(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from shelfhold.interfaces.cli import plugin_runner

    seen: dict[str, Any] = {}

    async def fake_serve(config: RunnerConfig, *, ready: Any = None) -> None:
        seen["config"] = config
        ready()

    monkeypatch.setattr(plugin_runner, "self_check", lambda **_kw: CLEAN)
    monkeypatch.setattr(plugin_runner, "serve", fake_serve)
    args = argparse.Namespace(
        socket=str(short_dir / "s.sock"), plugins=str(short_dir), check=False, core_uid=1000
    )
    assert plugin_runner.run(args) == 0
    assert seen["config"].socket_path == short_dir / "s.sock"
    assert str(short_dir / "s.sock") in capsys.readouterr().err

    # it changed between the two looks: refused all the same
    async def refusing(config: RunnerConfig, *, ready: Any = None) -> None:
        raise SelfCheckFailed(["it can see /data"])

    monkeypatch.setattr(plugin_runner, "serve", refusing)
    assert plugin_runner.run(args) == plugin_runner.REFUSED
    assert "/data" in capsys.readouterr().err
    # and --check never serves
    monkeypatch.setattr(plugin_runner, "serve", fake_serve)
    seen.clear()
    assert plugin_runner.run(argparse.Namespace(**{**vars(args), "check": True})) == 0
    assert seen == {}
