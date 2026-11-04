"""Export the FastAPI OpenAPI schema without starting a server or touching a database.

Usage: uv run --project backend python open-api/export_schema.py [output.json]
"""

import json
import sys
from collections.abc import Mapping
from pathlib import Path

from shelfhold import __version__
from shelfhold.interfaces.http_api.app import create_http_app


class _NoHealth:
    def migration_progress(self) -> tuple[int, int] | None:
        return None

    def readiness(self) -> Mapping[str, bool]:
        return {}


def main(argv: list[str]) -> None:
    app = create_http_app(health=_NoHealth(), on_startup=lambda: None, version=__version__)
    text = json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if len(argv) > 1:
        Path(argv[1]).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main(sys.argv)
