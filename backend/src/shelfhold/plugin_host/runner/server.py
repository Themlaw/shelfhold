"""The plugin runner: the companion that starts the isolated plugins' processes (D-P1).

It listens on one unix socket, on the volume it shares with the core, and the core connects: the
companion never connects to anything, and nothing of the core is reachable from here. Each
connection is one plugin. The first frame is the greeting (`runner_wire`): the companion checks who
is asking (the kernel's word for the peer's uid, never the core's), checks its own sandbox again,
checks the plugin is one it holds (a module of the volume's `site-packages`, by its name, never by
a path the core gives), and starts **one subprocess for it**, with a closed environment, its own
session, a core file of zero bytes, no more address space or CPU time than the companion allows,
and `umask 077` (`adapters.rpc.stdio`). After the answer, the connection is the subprocess's
standard streams, byte for byte: the companion carries `rpc_protocol` 1 without reading it.

A plugin's process lasts as long as its connection: when the core hangs up, or the plugin exits,
the process group is killed and the key is free again.

Imports the SDK, `plugin_host.rpc`, the `rpc_stdio` spawn and the standard library: the import
contract of the runner keeps everything else of the host out.
"""

import asyncio
import contextlib
import logging
import os
import signal
import socket
import stat
import struct
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from shelfhold.plugin_host.adapters.rpc.stdio import spawn_stdio
from shelfhold.plugin_host.rpc import codec, runner_wire
from shelfhold.plugin_host.rpc.protocol import Channel, RpcLimits
from shelfhold.plugin_host.runner.selfcheck import (
    Observation,
    SelfCheckFailed,
    observe,
    problems_of,
)

__all__ = ["RunnerConfig", "RunnerServer", "peer_uid_of", "serve"]

log = logging.getLogger("shelfhold.plugin_runner")

_FLOOR_MEMORY = 32 * 1024 * 1024
_GREETING_TIMEOUT = 10.0


@dataclass(frozen=True)
class RunnerConfig:
    socket_path: Path
    plugins_root: Path  # the volume: `site-packages` holds the installed wheels
    limits: RpcLimits = field(default_factory=RpcLimits)  # the ceilings; the core asks for less
    max_plugins: int = 16

    @property
    def site(self) -> Path:
        return self.plugins_root / "site-packages"


def peer_uid_of(connection: socket.socket) -> int:
    """The uid of the process at the other end of a unix socket, as the kernel reports it."""
    credentials = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    return int(struct.unpack("3i", credentials)[1])


class _Refused(Exception):
    pass


class RunnerServer:
    def __init__(
        self,
        config: RunnerConfig,
        *,
        own_uid: int | None = None,
        peer_uid: Callable[[socket.socket], int] | None = None,
        observe: Callable[[], Observation] | None = None,
    ) -> None:
        self._config = config
        self._own_uid = own_uid if own_uid is not None else os.getuid()
        self._peer_uid = peer_uid or peer_uid_of
        self._observe = observe or _default_observe
        self._server: asyncio.Server | None = None
        self._active: dict[str, Channel | None] = {}
        self._handlers: set[asyncio.Task[None]] = set()

    @property
    def config(self) -> RunnerConfig:
        return self._config

    def pids(self) -> dict[str, int]:
        """The running plugins and their processes."""
        return {k: c.pid for k, c in self._active.items() if c is not None and c.pid is not None}

    async def start(self) -> None:
        """Check the sandbox, then listen. A sandbox that is not one: `SelfCheckFailed`, and the
        socket is never bound."""
        problems = problems_of(await asyncio.to_thread(self._observe))
        if problems:
            raise SelfCheckFailed(problems)
        path = self._config.socket_path
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISSOCK(path.lstat().st_mode):
                path.unlink()  # a socket a crashed companion left: ours to remove, nothing else is
        old = os.umask(0)  # created world-writable: the core is another uid and must connect
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(path))
        finally:
            os.umask(old)
        path.chmod(0o666)
        log.info("plugin runner listening: uid=%d", self._own_uid)

    async def close(self) -> None:
        if self._active:
            log.info("plugin runner stopping: %s", ", ".join(sorted(self.pids())) or "no plugin")
        if self._server is not None:
            self._server.close()
        for task in list(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self._config.socket_path.unlink()

    # -- one connection, one plugin --------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        key: str | None = None
        channel: Channel | None = None
        try:
            key, channel = await self._greet(reader, writer)
            if channel is not None:
                await self._carry(reader, writer, channel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # whatever else: this plugin ends, the companion goes on
            log.warning("plugin runner: a connection ended badly: %s", type(exc).__name__)
        finally:
            if channel is not None:
                await asyncio.shield(channel.close())
            if key is not None:
                self._active.pop(key, None)
            with contextlib.suppress(Exception):
                writer.close()
            if task is not None:
                self._handlers.discard(task)

    async def _refuse(self, writer: asyncio.StreamWriter, number: int, reason: str) -> None:
        log.warning("plugin runner: greeting refused: %s", reason)
        with contextlib.suppress(Exception):
            await codec.write_frame(writer, runner_wire.refusal_message(number, reason))

    async def _greet(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[str | None, Channel | None]:
        number = 0
        try:
            payload = await asyncio.wait_for(
                codec.read_frame(reader, max_bytes=runner_wire.MAX_GREETING_BYTES),
                _GREETING_TIMEOUT,
            )
            if payload is None:  # it left without a word
                return None, None
            request = runner_wire.parse_start(codec.decode_payload(payload))
            number = request.number
        except (codec.FrameError, runner_wire.Refusal, TimeoutError) as exc:
            await self._refuse(writer, number, str(exc) or "no greeting")
            return None, None
        try:
            await self._admit(request, writer)
        except _Refused as exc:
            await self._refuse(writer, number, str(exc))
            return None, None
        key = request.key
        self._active[key] = None  # taken before anything is awaited: two greetings cannot race
        try:
            channel = await spawn_stdio(self._argv(request.entry), limits=self._limits(request))
        except Exception as exc:
            self._active.pop(key, None)
            await self._refuse(
                writer, number, f"the plugin could not be started: {type(exc).__name__}"
            )
            return None, None
        self._active[key] = channel
        log.info("plugin runner: %s started (pid %s)", key, channel.pid)
        await codec.write_frame(writer, runner_wire.ack_message(number, self._own_uid))
        return key, channel

    async def _admit(self, request: runner_wire.StartRequest, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        peer = self._peer_uid(sock)
        if peer == self._own_uid:
            raise _Refused("uid: the peer has the companion's own uid; only the core may ask")
        if request.core_uid != peer:
            raise _Refused("uid: the core's uid is not the one the kernel reports for the peer")
        problems = problems_of(await asyncio.to_thread(self._observe), core_uid=peer)
        if problems:
            raise _Refused("the sandbox is not one: " + "; ".join(problems))
        if request.key in self._active:
            raise _Refused(f"{request.key} is already running")
        if len(self._active) >= self._config.max_plugins:
            raise _Refused("too many plugins are running")
        if not self._installed(request.module):
            raise _Refused(f"{request.module} is not installed on the plugins volume")

    def _installed(self, module: str) -> bool:
        """The module's top-level name is something the volume holds, inside it."""
        site = self._config.site
        top = module.partition(".")[0]
        try:
            root = site.resolve(strict=True)
            for candidate in (site / top, site / f"{top}.py"):
                if candidate.exists() and candidate.resolve().parent == root:
                    return True
        except OSError:
            return False
        return False

    def _argv(self, entry: str) -> list[str]:
        return [
            sys.executable, "-I", "-m", "shelfhold.plugin_host.rpc.plugin_side",
            "--path", str(self._config.site), "--entry", entry,
        ]  # fmt: skip

    def _limits(self, request: runner_wire.StartRequest) -> RpcLimits:
        ceiling = self._config.limits
        return replace(
            ceiling,
            memory_bytes=max(_FLOOR_MEMORY, min(request.memory_bytes, ceiling.memory_bytes)),
            cpu_seconds=max(1, min(request.cpu_seconds, ceiling.cpu_seconds)),
        )

    async def _carry(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, channel: Channel
    ) -> None:
        """Both ways, byte for byte, until one side is gone."""

        async def to_plugin() -> None:
            while data := await reader.read(65536):
                channel.writer.write(data)
                await channel.writer.drain()

        async def to_core() -> None:
            while data := await channel.reader.read(65536):
                writer.write(data)
                await writer.drain()

        tasks = [asyncio.ensure_future(to_plugin()), asyncio.ensure_future(to_core())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def _default_observe() -> Observation:
    return observe()


async def serve(
    config: RunnerConfig,
    *,
    ready: Callable[[], Awaitable[object] | object] | None = None,
) -> None:
    """Run the companion until SIGTERM or SIGINT. `SelfCheckFailed` out of here is a refusal to
    start: nothing was bound."""
    server = RunnerServer(config)
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, stop.set)
    try:
        if ready is not None:
            done = ready()
            if isinstance(done, Awaitable):
                await done
        await stop.wait()
    finally:
        await server.close()
