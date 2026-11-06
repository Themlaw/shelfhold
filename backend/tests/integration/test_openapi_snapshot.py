"""The committed OpenAPI snapshot describes every route the running application serves."""

import json
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.main import create_app

SNAPSHOT = Path(__file__).parents[3] / "open-api" / "openapi.json"


@pytest.mark.req("EF-INT-001")
def test_openapi_snapshot_lists_every_route_of_the_application(settings: Settings) -> None:
    migrate(settings)
    served = {
        path for path in create_app(settings).openapi()["paths"] if path.startswith("/api/v1/")
    }
    documented = set(json.loads(SNAPSHOT.read_text(encoding="utf-8"))["paths"])
    assert "/api/v1/config" in served
    assert served - documented == set()
