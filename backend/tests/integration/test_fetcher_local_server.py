"""ENF-SEC-001: the real transport against a loopback server, allowed only by lan_targets."""

from __future__ import annotations

import asyncio
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from shelfhold.infrastructure.fetcher import (
    FetchUnreachable,
    GuardedFetcher,
    IntegrationFetcher,
    LanTargets,
    SsrfBlocked,
)
from tests.fakes_fetcher import FakeResolver


class _Echo(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
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
        f = IntegrationFetcher(dns, lan_targets=lan)
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


def _tls_material(directory: Path, name: str) -> tuple[Path, Path]:
    cert, key = directory / "cert.pem", directory / "key.pem"
    openssl = shutil.which("openssl") or "openssl"
    subprocess.run(  # noqa: S603
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-keyout", str(key), "-out", str(cert), "-subj", f"/CN={name}",
            "-addext", f"subjectAltName=DNS:{name}",
        ],
        check=True, capture_output=True,
    )  # fmt: skip
    return cert, key


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_https_to_the_pinned_ip_still_verifies_the_certificate_of_the_name(tmp_path: Path) -> None:
    cert, key = _tls_material(tmp_path, "secure.test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    client_context = ssl.create_default_context(cafile=str(cert))
    dns = FakeResolver({"secure.test": ["127.0.0.1"], "wrong.test": ["127.0.0.1"]})

    async def scenario() -> None:
        lan = await LanTargets.declare([f"secure.test:{port}", f"wrong.test:{port}"], dns)
        f = IntegrationFetcher(dns, lan_targets=lan, tls_context=client_context)
        ok = await f.get(f"https://secure.test:{port}/")
        assert ok.status == 200 and ok.body.startswith(b"secure.test")
        # same IP, another name: the certificate does not cover it, so TLS must fail
        with pytest.raises(FetchUnreachable):
            await f.get(f"https://wrong.test:{port}/")

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
