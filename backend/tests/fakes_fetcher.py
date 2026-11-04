"""Test doubles for the fetcher: a scripted DNS resolver and a recording mock transport."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import httpx2


class FakeResolver:
    """Scripted DNS. A value may be a list (always the same) or a list of lists (one per call)."""

    def __init__(self, table: dict[str, Sequence[str] | Sequence[Sequence[str]]]) -> None:
        self.table = table
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        answers = self.table[host]
        if answers and not isinstance(answers[0], str):
            nth = sum(1 for h, _ in self.calls if h == host) - 1
            return list(answers[min(nth, len(answers) - 1)])  # type: ignore[arg-type]
        return list(answers)  # type: ignore[arg-type]


class Recorder:
    """A MockTransport handler that records what the fetcher actually sent on the wire."""

    def __init__(self, respond: Callable[[httpx2.Request], httpx2.Response] | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self._respond = respond or (lambda r: httpx2.Response(200, content=b"ok"))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._respond(request)

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)

    @property
    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]
