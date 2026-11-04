"""The backend serves the built SPA (ADR-0013); the API keeps priority over the fallback."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.main import create_app

INDEX = "<!doctype html><title>Shelfhold</title>"


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    build = tmp_path / "build"
    (build / "_app").mkdir(parents=True)
    (build / "index.html").write_text(INDEX, encoding="utf-8")
    (build / "_app" / "app.js").write_text("export {};", encoding="utf-8")
    return build


def _client(settings: Settings, build: Path | None) -> TestClient:
    migrate(settings)
    return TestClient(create_app(settings.model_copy(update={"frontend_dir": build})))


def test_index_html_served_at_root_when_build_present(settings: Settings, build_dir: Path) -> None:
    with _client(settings, build_dir) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["content-type"].startswith("text/html")


def test_static_asset_served(settings: Settings, build_dir: Path) -> None:
    with _client(settings, build_dir) as client:
        response = client.get("/_app/app.js")
    assert response.status_code == 200
    assert response.text == "export {};"


def test_unknown_client_route_falls_back_to_index(settings: Settings, build_dir: Path) -> None:
    with _client(settings, build_dir) as client:
        response = client.get("/library/42")
    assert response.status_code == 200
    assert response.text == INDEX


def test_api_routes_win_over_the_fallback(settings: Settings, build_dir: Path) -> None:
    with _client(settings, build_dir) as client:
        response = client.get("/healthz")
    assert response.json()["state"] == "ok"


def test_unknown_api_path_is_404_not_index(settings: Settings, build_dir: Path) -> None:
    with _client(settings, build_dir) as client:
        response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert INDEX not in response.text


def test_path_traversal_is_refused(settings: Settings, build_dir: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    with _client(settings, build_dir) as client:
        response = client.get("/..%2fsecret.txt")
    assert response.text != "secret"


def test_without_build_only_the_api_answers(settings: Settings, tmp_path: Path) -> None:
    with _client(settings, tmp_path / "absent") as client:
        assert client.get("/").status_code == 404
        assert client.get("/healthz").status_code == 200


def test_openapi_describes_healthz_response(settings: Settings) -> None:
    with _client(settings, None) as client:
        schema = client.get("/openapi.json").json()
    ok = schema["paths"]["/healthz"]["get"]["responses"]["200"]["content"]["application/json"]
    assert "$ref" in ok["schema"]
