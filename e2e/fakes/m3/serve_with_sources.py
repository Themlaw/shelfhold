"""Harness of the M3 journey: `shelfhold serve`, and the two addresses of the books project set once
the server answers.

Not part of the product and not an action of the person being timed. The simulated catalogue and
mirror live at addresses that exist only in this test stack; a real install talks to the real
project and types nothing for it. The journey of L57 counts every gesture of the administrator
(ENF-UX-001, "sans éditer d'autre fichier que le compose"), so these two settings are made here, by
the stack, instead of by a command typed in the test. The retry covers the start of the server (and
a server that refuses until the first account exists): it stops at the first success.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from types import FrameType

SETTINGS = [
    'catalog_url="http://catalog.example/cache/epub/feeds/pg_catalog.csv"',
    'mirror_url="http://aleph.gutenberg.org"',
]
GIVE_UP_S = 1500


def main() -> int:
    server = subprocess.Popen(["shelfhold", "serve"])  # noqa: S603, S607

    def forward(number: int, _frame: FrameType | None) -> None:
        server.send_signal(number)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    deadline = time.monotonic() + GIVE_UP_S
    while server.poll() is None and time.monotonic() < deadline:
        done = subprocess.run(  # noqa: S603
            ["shelfhold", "plugin", "set", "gutenberg", *SETTINGS],  # noqa: S607
            capture_output=True, check=False, timeout=60,
        )  # fmt: skip
        if done.returncode == 0:
            break
        time.sleep(2)
    return server.wait()


if __name__ == "__main__":
    sys.exit(main())
