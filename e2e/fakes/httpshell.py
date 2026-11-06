"""Minimal HTTP shell for the E2E fakes: one request in, one (status, headers, body) out.

Standard library only. The fakes of `backend/tests/fakes` hold the behaviour; this file only
lends them a socket, inside the E2E network namespace (nothing leaves it).
"""

from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Handler = Callable[[str, str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]]


def serve(host: str, port: int, handle: Handler, banner: str) -> None:
    class Shell(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _answer(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, out_headers, out = handle(self.command, self.path, headers, body)
            self.send_response(status)
            for name, value in out_headers.items():
                self.send_header(name, value)
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(out)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _answer  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            print(f"{banner} {self.command} {self.path.split('?')[0]}", flush=True)

    print(f"{banner} listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Shell).serve_forever()
