"""Simulated Kobo client for the M6 E2E (L27). Synthetic data only."""

from __future__ import annotations

import json
import sys


def main() -> None:
    print(json.dumps({"initialized": True, "pages": 1}))


if __name__ == "__main__":
    main()
