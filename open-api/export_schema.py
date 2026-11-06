"""Export the FastAPI OpenAPI schema without starting a server or touching a database.

Usage: uv run --project backend python open-api/export_schema.py [output.json]
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from shelfhold.config import load_settings
from shelfhold.main import create_app


def build_schema() -> dict[str, Any]:
    """The schema of the application as `shelfhold serve` builds it (every router of the
    composition root), without starting a server: the database is opened at the first request."""
    with tempfile.TemporaryDirectory() as scratch:
        env = {
            "SHELFHOLD_DATA_DIR": f"{scratch}/data",
            "SHELFHOLD_STORE_DIR": f"{scratch}/store",
        }
        return create_app(load_settings(env).values).openapi()


def main(argv: list[str]) -> None:
    text = json.dumps(build_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if len(argv) > 1:
        Path(argv[1]).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main(sys.argv)
