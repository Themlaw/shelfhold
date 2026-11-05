"""Fixture server of the synthetic Suwayomi extension (ENF-LEG-013): invented series, generated pages.

Standard library only. GET /series, /series/<slug>/chapters, /pages/<slug>/<chapter>
(a list of page URLs) and /image/<slug>/<chapter>/<n>.png (a generated PNG). Run:
`python serve.py [port]` (default 8000); it logs each request, nothing else.
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERIES = {
    "synthetic-alpha": {"title": "Synthetic Alpha", "chapters": 3},
    "synthetic-beta": {"title": "Synthetic Beta", "chapters": 1},
}
PAGES = 3


def png(width: int, height: int, shade: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(
        b"\x00" + bytes([(shade + x * 7) % 256 for x in range(width)]) for _ in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object) -> None:
        self._send(200, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if parts == ["series"]:
            self._json([{"slug": s, "title": v["title"]} for s, v in SERIES.items()])
        elif (
            len(parts) == 3
            and parts[0] == "series"
            and parts[1] in SERIES
            and parts[2] == "chapters"
        ):
            count = SERIES[parts[1]]["chapters"]
            self._json(
                [
                    {"id": f"ch-{n:03d}", "name": f"Chapter {n}", "number": n}
                    for n in range(1, count + 1)
                ]
            )
        elif len(parts) == 3 and parts[0] == "pages" and parts[1] in SERIES:
            base = f"http://fixtures:{self.server.server_port}/image/{parts[1]}/{parts[2]}"
            self._json([f"{base}/{n}.png" for n in range(1, PAGES + 1)])
        elif (
            len(parts) == 4
            and parts[0] == "image"
            and parts[1] in SERIES
            and parts[3].endswith(".png")
        ):
            shade = (len(parts[1]) * 31 + int(parts[3].split(".")[0]) * 40) % 256
            self._send(200, png(24, 32, shade), "image/png")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, format: str, *args: object) -> None:
        print(f"fixtures {self.command} {self.path.split('?')[0]}", flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104
