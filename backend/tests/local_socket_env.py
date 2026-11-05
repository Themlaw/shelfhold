"""A local-socket server running on its own event loop in a thread, for synchronous tests."""

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from shelfhold.infrastructure.local_socket import Handler, LocalSocketServer, Peer, RpcError


async def echo(peer: Peer, method: str, params: dict[str, Any]) -> object:
    if method == "whoami":
        return {"uid": peer.uid, "pid": peer.pid}
    if method == "sleep":
        await asyncio.sleep(float(params["seconds"]))
        return "late"
    if method == "echo":
        return params
    raise RpcError(-32601, f"unknown method {method}")


@contextmanager
def serving(path: Path, handler: Handler = echo, **options: Any) -> Iterator[LocalSocketServer]:
    loop = asyncio.new_event_loop()
    server = LocalSocketServer(path, handler, **options)
    ready = threading.Event()
    failure: list[BaseException] = []

    def run() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.start())
        except BaseException as error:  # reported to the test thread
            failure.append(error)
            ready.set()
            return
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(10)
    if failure:
        loop.close()
        raise failure[0]
    try:
        yield server
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()
