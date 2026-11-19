"""The SPA fallback treats the feed prefixes (OPDS, kosync) as API segments."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.main import create_app

pytestmark = pytest.mark.req("EF-LEC-009", "EF-INT-007")  # the OPDS feed and the kosync server

CSP = "default-src 'self'; script-src 'self' 'sha256-abc='"
INDEX = (
    f'<!doctype html><meta http-equiv="content-security-policy" content="{CSP}">'
    "<title>Shelfhold</title>"
)


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    build = tmp_path / "build"
    (build / "_app" / "immutable").mkdir(parents=True)
    (build / "_app" / "immutable" / "chunk.js").write_text("export {};", encoding="utf-8")
    (build / "index.html").write_text(INDEX, encoding="utf-8")
    (build / "_app" / "app.js").write_text("export {};", encoding="utf-8")
    return build


def _client(settings: Settings, build: Path) -> TestClient:
    migrate(settings)
    return TestClient(create_app(settings.model_copy(update={"frontend_dir": build})))


@pytest.mark.parametrize(
    "path",
    [
        "/opds/nope",
        "/opds/foo/bar",
        "/kosync/nope",
        "/kosync/foo/bar",
    ],
)
def test_feed_segments_are_404_not_index(settings: Settings, build_dir: Path, path: str) -> None:
    with _client(settings, build_dir) as client:
        response = client.get(path)
    assert response.status_code == 404
    assert INDEX not in response.text
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("path", ["/opds/nope", "/kosync/nope"])
def test_feed_segment_404_carries_no_store(settings: Settings, build_dir: Path, path: str) -> None:
    with _client(settings, build_dir) as client:
        response = client.get(path, headers={"X-Request-ID": "req-feed-1"})
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == "req-feed-1"
    assert response.json()["code"] == "not_found"
