"""The core's end of the plugin runner's socket (D-P1, EF-SRC-016).

``connect_runner`` opens a connection to the companion, says which plugin it wants (`runner_wire`)
and, once the companion has answered, gives back a `Channel` that `RpcSession.open` speaks
`rpc_protocol` 1 over. The core connects, and only connects: it creates nothing on the volume it
shares with the companion, binds nothing and listens to nothing; a companion that is not there is
`RunnerUnavailable`, a refusal of the companion is `RunnerRefused` with its reason. Nothing of the
CLI's socket is used here.
"""

import asyncio
import contextlib
import os
import socket
import stat
import struct
from pathlib import Path

from shelfhold.plugin_host.rpc import codec, runner_wire
from shelfhold.plugin_host.rpc.protocol import Channel, PluginFailed, RpcError, RpcLimits

__all__ = ["DEFAULT_SOCKET", "RUNNER_UID", "RunnerRefused", "RunnerUnavailable", "connect_runner"]

DEFAULT_SOCKET = "/plugin-rpc/runner.sock"
RUNNER_UID = 1001  # the companion's uid in the image (ADR-0056): the one the core speaks to


class RunnerUnavailable(PluginFailed):
    """There is no companion to ask: the plugin is not started."""


class RunnerRefused(RpcError):
    """The companion refused the plugin, and says why."""


def _listener_uid(writer: asyncio.StreamWriter) -> int | None:
    """The uid of the process that listens at the other end, from the kernel (`SO_PEERCRED`)."""
    sock = writer.get_extra_info("socket")
    try:
        credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return int(struct.unpack("3i", credentials)[1])
    except (AttributeError, OSError, struct.error):
        return None


async def connect_runner(
    socket_path: str | Path,
    *,
    key: str,
    entry: str,
    limits: RpcLimits,
    core_uid: int | None = None,
    runner_uid: int | None = None,
    timeout: float = 15.0,
) -> Channel:
    """Ask the companion to start plugin `key` (`entry` is `module:attr`) and return the channel to
    it. `core_uid` is this process's own uid (read here, never given by a caller in production).
    `runner_uid`, when given, is the uid the companion must have, as the kernel reports it for the
    socket (`SO_PEERCRED`): a listener of another uid is not spoken to, and nothing is sent."""
    uid = os.getuid() if core_uid is None else core_uid
    try:
        kind = os.lstat(socket_path).st_mode  # not followed: a link is somebody's, not the socket
    except OSError as exc:
        raise RunnerUnavailable(
            f"the plugin runner is not reachable: {type(exc).__name__}"
        ) from exc
    if not stat.S_ISSOCK(kind):
        raise RunnerUnavailable("the plugin runner's socket is not a socket: nothing was sent")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)), timeout
        )
    except (OSError, TimeoutError) as exc:  # nothing listens there: a missing file, a stray file
        raise RunnerUnavailable(
            f"the plugin runner is not reachable: {type(exc).__name__}"
        ) from exc

    if runner_uid is not None:
        seen = _listener_uid(writer)
        if seen != runner_uid:
            writer.close()
            raise RunnerUnavailable(
                f"the plugin runner's socket is served by uid {seen}, not {runner_uid}: "
                "nothing was sent"
            )

    async def close() -> None:
        with contextlib.suppress(Exception):
            writer.close()
            # shielded: a wait that times out must not cancel the transport's close future, which a
            # second `close()` awaits too
            await asyncio.wait_for(asyncio.shield(writer.wait_closed()), 5.0)

    async def exit_reason() -> str | None:
        return "the plugin runner closed the connection (its log says why)"

    try:
        greeting = runner_wire.start_message(
            key=key,
            entry=entry,
            core_uid=uid,
            memory_bytes=limits.memory_bytes,
            cpu_seconds=limits.cpu_seconds,
        )
        await codec.write_frame(writer, greeting)
        payload = await asyncio.wait_for(codec.read_frame(reader), timeout)
        if payload is None:
            raise RunnerUnavailable("the plugin runner closed the connection without answering")
        answer = codec.decode_payload(payload)
    except (codec.FrameError, OSError, TimeoutError) as exc:
        await close()
        raise RunnerUnavailable(f"the plugin runner did not answer: {type(exc).__name__}") from exc
    except BaseException:
        await close()
        raise
    error = answer.get("error") if isinstance(answer, dict) else None
    if isinstance(error, dict):
        await close()
        raise RunnerRefused(str(error.get("message", "refused"))[:500])
    if not (isinstance(answer, dict) and isinstance(answer.get("result"), dict)):
        await close()
        raise RunnerUnavailable("the plugin runner's answer is not one")
    return Channel(reader, writer, close, exit_reason)
