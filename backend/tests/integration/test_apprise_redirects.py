"""ENF-SEC-016 (passe 11): a service that answers with a cross-origin redirect gets no credentials.

Apprise makes its own requests (it does not go through the Fetcher, EF-INT-006). The limit is
documented in docs/admin/notifications.md; what is tested here is that Shelfhold never lets it
follow a redirect, so `X-Gotify-Key`-style headers cannot travel to another origin (CVE-2026-59180).
Two loopback servers play the notification service and the redirect target.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from shelfhold.integrations.apprise import Message, send_with_apprise

SECRET = "tkn-synthetic-redirect-0001"  # noqa: S105


class Target(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, str]]] = []

    def do_POST(self) -> None:
        self.seen.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST

    def log_message(self, *args: object) -> None:
        pass


def _server(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture
def servers() -> Iterator[tuple[int, int, list[dict[str, str]]]]:
    Target.seen = []
    target, target_port = _server(Target)

    class Redirector(BaseHTTPRequestHandler):
        hits = 0

        def do_POST(self) -> None:
            type(self).hits += 1
            self.send_response(307)
            self.send_header("Location", f"http://localhost:{target_port}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *args: object) -> None:
            pass

    redirector, redirector_port = _server(Redirector)
    yield redirector_port, target_port, Target.seen
    for server in (redirector, target):
        server.shutdown()
        server.server_close()


@pytest.mark.req("ENF-SEC-016", "EF-INT-006")
def test_apprise_floor_and_no_auth_headers_on_redirect(
    servers: tuple[int, int, list[dict[str, str]]],
) -> None:
    redirector_port, _target_port, received = servers
    for url in (
        f"gotify://127.0.0.1:{redirector_port}/{SECRET}",
        f"gotify://127.0.0.1:{redirector_port}/{SECRET}?redirect=yes",  # the URL cannot opt in
        f"ntfy://user:{SECRET}@127.0.0.1:{redirector_port}/topic",
    ):
        result = send_with_apprise(url, Message("Shelfhold", "disk.low"))
        assert result.ok is False  # a 307 is a failure, not something to chase
    assert received == []  # the target never saw a request, so no header, no body
    for request in received:
        assert SECRET not in "".join(request.values())
