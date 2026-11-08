"""Synthetic stand-in for the Sablier API (L21): the blocking strategy and nothing else."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx2

from shelfhold.infrastructure.fetcher.client import IntegrationFetcher
from shelfhold.infrastructure.fetcher.lan import LanTargets
from tests.fakes_fetcher import FakeResolver

BASE_URL = "http://sablier.lan:10000"
LAN_ADDRESS = "192.168.1.60"
BLOCKING_PATH = "/api/strategies/blocking"


@dataclass
class SablierCall:
    path: str
    names: list[str]
    group: str | None
    session_duration: str | None
    timeout: str | None
    headers: dict[str, str]


@dataclass
class FakeSablier:
    """Starts a stopped container when asked and answers once it is ready (blocking strategy).

    `starts[name]` is what the container does when it is woken (the simulated tool comes up).
    `slow`: the container never gets ready within the delay, `unreachable`: Sablier is down,
    `redirect_to`: Sablier answers a redirection (the guard must refuse a private target).
    """

    starts: dict[str, Callable[[], None]] = field(default_factory=dict)
    slow: bool = False
    unreachable: bool = False
    redirect_to: str | None = None
    calls: list[SablierCall] = field(default_factory=list)

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.unreachable:
            raise httpx2.ConnectError("connection refused", request=request)
        params = request.url.params
        self.calls.append(
            SablierCall(
                request.url.path,
                params.get_list("names"),
                params.get("group"),
                params.get("session_duration"),
                params.get("timeout"),
                {k.lower(): v for k, v in request.headers.items()},
            )
        )
        if request.url.path != BLOCKING_PATH:
            return httpx2.Response(404, text="unknown strategy")
        if self.redirect_to is not None:
            return httpx2.Response(302, headers={"location": self.redirect_to})
        names = params.get_list("names") or [f"group:{params.get('group')}"]
        status = "not-ready" if self.slow else "ready"
        if not self.slow:
            for name in names:
                self.starts.get(name, lambda: None)()
        body = {
            "session": {
                "instances": [
                    {"instance": {"name": n, "status": status}, "error": None} for n in names
                ],
                "status": status,
            }
        }
        return httpx2.Response(200, content=json.dumps(body).encode())

    def fetcher(self) -> IntegrationFetcher:
        """The production fetcher; the only reachable host is the simulated Sablier."""
        resolver = FakeResolver({"sablier.lan": [LAN_ADDRESS]})
        lan = asyncio.run(LanTargets.declare(["sablier.lan:10000"], resolver))
        return IntegrationFetcher(resolver, lan_targets=lan, transport=self.transport)
