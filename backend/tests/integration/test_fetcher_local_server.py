"""ENF-SEC-001: the real transport against a server bound to 127.0.0.1, allowed only by lan_targets."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from shelfhold.infrastructure.fetcher import GuardedFetcher, LanTargets, SsrfBlocked
from tests.fakes_fetcher import FakeResolver


class _Echo(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/redirect-out":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/")
            self.end_headers()
            return
        body = f"{self.headers['Host']}|{self.headers.get('Authorization', '')}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence
        pass


@pytest.fixture
def port() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(5)


@pytest.mark.req("ENF-SEC-001")
def test_loopback_server_reached_only_through_an_explicit_lan_target(port: int) -> None:
    dns = FakeResolver({"suwayomi.lan": ["127.0.0.1"]})

    async def scenario() -> None:
        lan = await LanTargets.declare([f"suwayomi.lan:{port}"], dns)
        f = GuardedFetcher(dns, lan_targets=lan)
        response = await f.get(
            f"http://suwayomi.lan:{port}/x", headers={"Authorization": "Bearer t"}
        )
        assert response.body == f"suwayomi.lan:{port}|Bearer t".encode()  # real Host, real auth
        with pytest.raises(SsrfBlocked):  # redirect to the metadata address is revalidated
            await f.get(f"http://suwayomi.lan:{port}/redirect-out")
        with pytest.raises(SsrfBlocked):  # the same server without a declaration
            await GuardedFetcher(dns).get(f"http://suwayomi.lan:{port}/x")
        with pytest.raises(SsrfBlocked):
            await GuardedFetcher(dns).get(f"http://127.0.0.1:{port}/x")

    asyncio.run(scenario())
