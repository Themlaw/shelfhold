"""A simulated provider that replays a fabricated cassette (`record_mode` none: nothing leaves).

A request is answered by the first interaction of the cassette with the same method, the same
address and a body that holds the text the interaction asks for (`body_contains`); anything else
is a test failure, never a network call. Every request is recorded with its fake instant so that
a test can prove the rate. Synthetic data only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from tests.fakes_fetcher import FakeResolver

CASSETTES = Path(__file__).resolve().parent.parent / "cassettes"
PUBLIC = "93.184.216.34"


@dataclass
class Seen:
    method: str
    host: str
    path: str
    body: str
    headers: dict[str, str]
    at: float


@dataclass
class CassetteServer:
    host: str
    cassettes: tuple[Path, ...]
    clock: Callable[[], float] = lambda: 0.0
    # What a test may force: the announced limit (0: none), a status for every answer,
    # a Retry-After.
    limit: int | None = None
    status: int | None = None
    retry_after: int | None = None
    seen: list[Seen] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._interactions: list[dict[str, Any]] = []
        for path in self.cassettes:
            self._interactions += json.loads(path.read_text(encoding="utf-8"))["interactions"]

    def use(self, name: str) -> None:
        """Answer from another cassette of the same provider from now on."""
        folder = self.cassettes[0].parent
        self._interactions = json.loads((folder / f"{name}.json").read_text("utf-8"))[
            "interactions"
        ]
        self.status = self.retry_after = None

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    @property
    def resolver(self) -> FakeResolver:
        return FakeResolver({self.host: [PUBLIC]})

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        host, path = request.headers["host"], request.url.path
        body = request.content.decode("utf-8", "replace")
        self.seen.append(
            Seen(request.method, host, path, body, dict(request.headers), self.clock())
        )
        if path == "/robots.txt":
            return httpx2.Response(404)
        for item in self._interactions:
            asked = item["request"]
            if (
                asked["method"] == request.method
                and asked["uri"].split("://", 1)[1].split("/", 1)[0] == host
                and "/" + asked["uri"].split("://", 1)[1].split("/", 1)[1] == path
                and asked.get("body_contains", "") in body
            ):
                return self._answer(item["response"])
        raise AssertionError(f"no recorded answer for {request.method} {host}{path} {body[:80]}")

    def _answer(self, recorded: dict[str, Any]) -> httpx2.Response:
        headers = dict(recorded.get("headers", {}))
        if self.limit == 0:  # a test asks for an answer that announces nothing
            headers.pop("X-RateLimit-Limit", None)
            headers.pop("X-RateLimit-Remaining", None)
        elif self.limit is not None:
            headers["X-RateLimit-Limit"] = str(self.limit)
        if self.retry_after is not None:
            headers["Retry-After"] = str(self.retry_after)
        status = self.status if self.status is not None else recorded["status"]
        return httpx2.Response(status, headers=headers, content=recorded["body"].encode())

    def calls(self) -> list[Seen]:
        return [s for s in self.seen if s.path != "/robots.txt"]
