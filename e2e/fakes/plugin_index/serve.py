"""Simulated signed plugin index for the M6 E2E (L27). Synthetic data only."""

from __future__ import annotations

import http.server
import socketserver

PORT = 8081


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"format": 1, "version": 1, "plugins": []}')


if __name__ == "__main__":
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:  # noqa: S104
        httpd.serve_forever()
