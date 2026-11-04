"""Container healthcheck without curl: exit 0 when /readyz answers 200 on loopback."""

import os
import sys
import urllib.error
import urllib.request

port = os.environ.get("SHELFHOLD_PORT", "8080")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=4) as response:  # noqa: S310
        sys.exit(0 if response.status == 200 else 1)
except (urllib.error.URLError, OSError):
    sys.exit(1)
