"""An SSE stream read through the real application, without a socket and without sleeping.

`TestClient` buffers a response until the application is done, so it cannot read a stream that
never ends. `open_stream` drives the ASGI application itself: it feeds the request, collects the
frames as they are sent and sends the disconnect once `until` is satisfied. The stream's wait
(`stream._wait`) is replaced by a script: each time the stream would sleep, the next step of the
script runs (it publishes a row, revokes a session...), then the test clock moves by the time the
stream asked to wait. The only real timeout is the safety net that fails a hung test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

import shelfhold.interfaces.http_api.stream as stream_module
from tests.auth_env import ManualClock

PATH = "/api/v1/events"


@dataclass
class Received:
    status: int
    headers: dict[str, str]
    frames: list[str] = field(default_factory=list)
    complete: bool = False  # the application ended the response by itself
    waits: list[float] = field(default_factory=list)  # what the stream asked to wait

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        """`(event name, data)` of the frames that carry a payload."""
        out = []
        for frame in self.frames:
            lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
            if "event" in lines and "data" in lines:
                out.append((lines["event"], json.loads(lines["data"])))
        return out

    def ids(self) -> list[str]:
        return [
            line.removeprefix("id: ")
            for frame in self.frames
            for line in frame.splitlines()
            if line.startswith("id: ")
        ]

    def event_ids(self) -> list[str]:
        """The ids of the frames that carry a payload (not the heartbeats')."""
        return [
            line.removeprefix("id: ")
            for frame in self.frames
            if "\nevent: " in frame
            for line in frame.splitlines()
            if line.startswith("id: ")
        ]

    def heartbeats(self) -> list[str]:
        return [f for f in self.frames if ": keepalive" in f]


def cookie_header(client: Any) -> str:
    return "; ".join(f"{name}={value}" for name, value in client.cookies.items())


def open_stream(
    app: Any,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cookie: str = "",
    headers: dict[str, str] | None = None,
    script: Iterable[Callable[[], None]] = (),
    until: Callable[[Received], bool] = lambda received: False,
    timeout: float = 20.0,
) -> Received:
    """Open `GET /api/v1/events` and read until `until(received)` or the response ends."""
    steps: Iterator[Callable[[], None]] = iter(script)
    received = Received(0, {})

    async def fake_wait(_wake: asyncio.Event, seconds: float) -> None:
        received.waits.append(seconds)
        step = next(steps, None)
        if step is not None:
            step()
        clock.advance(timedelta(seconds=seconds))

    monkeypatch.setattr(stream_module, "_wait", fake_wait)
    raw_headers = [(b"host", b"testserver")]
    if cookie:
        raw_headers.append((b"cookie", cookie.encode()))
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "https", "path": PATH, "raw_path": PATH.encode(), "query_string": b"",
        "headers": raw_headers, "client": ("10.0.0.9", 40000), "server": ("testserver", 443),
        "root_path": "", "state": {},
    }  # fmt: skip

    async def run() -> None:
        disconnect = asyncio.Event()
        sent_request = False
        buffer = ""

        async def receive() -> dict[str, Any]:
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal buffer
            if message["type"] == "http.response.start":
                received.status = message["status"]
                received.headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
            elif message["type"] == "http.response.body":
                buffer += message.get("body", b"").decode()
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    received.frames.append(frame + "\n\n")
                if until(received):
                    disconnect.set()

        await asyncio.wait_for(app(scope, receive, send), timeout)
        received.complete = not disconnect.is_set()

    asyncio.run(run())
    return received
