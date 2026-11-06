"""EF-INT-001: `open-api/openapi.json` is the OpenAPI 3.1 document of the application itself."""

from __future__ import annotations

import json
import sys

import pytest

from tests.tools_loader import REPO, load_tool

od = load_tool("openapi_diff")


def _generated() -> dict[str, object]:
    sys.path.insert(0, str(REPO / "open-api"))
    try:
        import export_schema  # type: ignore[import-not-found]
    finally:
        sys.path.remove(str(REPO / "open-api"))
    return json.loads(json.dumps(export_schema.build_schema()))  # type: ignore[no-any-return]


@pytest.mark.req("EF-INT-001")
def test_openapi_snapshot_equals_application() -> None:
    committed = json.loads((REPO / "open-api" / "openapi.json").read_text(encoding="utf-8"))
    generated = _generated()
    assert committed["openapi"].startswith("3.1")
    changes = od.diff(committed, generated)  # the whole document, both ways
    assert not changes, (
        "open-api/openapi.json is not the application's document; run ./open-api/generate.sh\n"
        + "\n".join(str(c) for c in changes[:40])
    )


@pytest.mark.req("EF-INT-001")
def test_openapi_snapshot_covers_every_router_of_the_composition_root() -> None:
    committed = json.loads((REPO / "open-api" / "openapi.json").read_text(encoding="utf-8"))
    assert {"/api/v1/status", "/api/v1/jobs", "/api/v1/config"} <= set(committed["paths"])
