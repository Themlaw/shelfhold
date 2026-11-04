"""ENF-SEC-004 / ENF-PRIV-002: one middleware sets the discretion headers on every response."""

from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shelfhold.interfaces.http_api.app import create_http_app
from shelfhold.interfaces.http_api.content import content_response

CSP = "default-src 'self'; script-src 'self' 'sha256-abc='"
INDEX = (
    f'<!doctype html><meta http-equiv="content-security-policy" content="{CSP}">'
    "<title>Shelfhold</title>"
)


class _Health:
    def __init__(self, *, progress: tuple[int, int] | None = None, broken: bool = False) -> None:
        self._progress = progress
        self._broken = broken

    def migration_progress(self) -> tuple[int, int] | None:
        return self._progress

    def readiness(self) -> Mapping[str, bool]:
        if self._broken:
            raise RuntimeError("probe exploded")
        return {"database_writable": True}


def _app(tmp_path: Path, *, with_spa: bool = True, health: _Health | None = None) -> FastAPI:
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    (build / "index.html").write_text(INDEX, encoding="utf-8")
    return create_http_app(
        health=health or _Health(),
        on_startup=lambda: None,
        version="9.9.9",
        frontend_dir=build if with_spa else None,
    )


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


ROUTES = [
    ("GET", "/healthz"),
    ("GET", "/readyz"),
    ("GET", "/robots.txt"),
    ("GET", "/"),
    ("GET", "/library/42"),
    ("HEAD", "/"),
    ("GET", "/api/v1/nope"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("POST", "/healthz"),
    ("OPTIONS", "/healthz"),
]


@pytest.mark.req("ENF-PRIV-002", "ENF-SEC-004")
@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_discretion_headers_on_every_route(tmp_path: Path, method: str, path: str) -> None:
    with _client(_app(tmp_path)) as client:
        response = client.request(method, path)
    headers = response.headers
    assert headers["x-robots-tag"] == "noindex, nofollow"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


@pytest.mark.req("ENF-PRIV-002", "ENF-SEC-004")
def test_discretion_headers_on_unhandled_error(tmp_path: Path) -> None:
    with _client(_app(tmp_path, health=_Health(broken=True))) as client:
        response = client.get("/readyz")
    assert response.status_code == 500
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.req("ENF-SEC-004")
def test_json_responses_have_nosniff_and_referrer_policy(tmp_path: Path) -> None:
    with _client(_app(tmp_path, with_spa=False)) as client:
        response = client.get("/healthz")
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.req("ENF-SEC-004")
def test_spa_csp_hash_is_kept_and_extended_with_frame_ancestors(tmp_path: Path) -> None:
    with _client(_app(tmp_path)) as client:
        response = client.get("/")
        api = client.get("/healthz")
    assert response.headers["content-security-policy"] == f"{CSP}; frame-ancestors 'none'"
    assert response.headers["cache-control"] == "no-cache"
    assert api.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]


@pytest.mark.req("ENF-PRIV-002")
def test_robots_txt_disallows_everything(tmp_path: Path) -> None:
    with _client(_app(tmp_path)) as client:
        response = client.get("/robots.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.splitlines() == ["User-agent: *", "Disallow: /"]


@pytest.mark.req("ENF-PRIV-002")
def test_healthz_hides_version_from_anonymous(tmp_path: Path) -> None:
    with _client(_app(tmp_path, with_spa=False)) as client:
        ok = client.get("/healthz")
    with _client(_app(tmp_path, with_spa=False, health=_Health(progress=(1, 4)))) as client:
        migrating = client.get("/healthz")
    assert ok.json() == {"state": "ok"}
    assert migrating.json() == {"state": "migrating", "progress": {"done": 1, "total": 4}}
    assert "9.9.9" not in ok.text + migrating.text


@pytest.mark.req("ENF-PRIV-002")
@pytest.mark.parametrize("path", ["/openapi.json", "/api/v1/openapi.json"])
@pytest.mark.parametrize("with_spa", [True, False])
def test_openapi_json_not_served_anonymously(tmp_path: Path, path: str, with_spa: bool) -> None:
    with _client(_app(tmp_path, with_spa=with_spa)) as client:
        response = client.get(path)
    assert response.status_code == 404
    assert "9.9.9" not in response.text


@pytest.mark.req("ENF-PRIV-002")
@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_no_interactive_docs(tmp_path: Path, path: str) -> None:
    with _client(_app(tmp_path, with_spa=False)) as client:
        assert client.get(path).status_code == 404


def test_offline_schema_export_still_works(tmp_path: Path) -> None:
    schema = _app(tmp_path).openapi()
    assert schema["info"]["version"] == "9.9.9"
    assert "/healthz" in schema["paths"]


@pytest.mark.req("ENF-SEC-004")
@pytest.mark.parametrize(
    ("media_type", "filename"),
    [("image/svg+xml", "cover.svg"), ("text/html", "page.html")],
)
def test_svg_served_as_attachment_with_csp_sandbox(
    tmp_path: Path, media_type: str, filename: str
) -> None:
    app = _app(tmp_path, with_spa=False)

    @app.get("/untrusted", include_in_schema=False)
    def untrusted() -> object:
        return content_response(b"<svg onload='x()'/>", media_type=media_type, filename=filename)

    with _client(app) as client:
        response = client.get("/untrusted")
    assert response.headers["content-security-policy"].startswith("sandbox")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["content-disposition"] == f'attachment; filename="{filename}"'
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.req("ENF-SEC-004")
def test_other_untrusted_content_is_sandboxed_but_inline(tmp_path: Path) -> None:
    response = content_response(b"\x89PNG", media_type="image/png", filename="cover.png")
    assert response.headers["content-security-policy"] == "sandbox"
    assert "content-disposition" not in response.headers


@pytest.mark.req("EF-INT-001")
def test_cors_closed_by_default(tmp_path: Path) -> None:
    with _client(_app(tmp_path)) as client:
        simple = client.get("/healthz", headers={"Origin": "https://evil.example"})
        preflight = client.options(
            "/healthz",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
    for response in (simple, preflight):
        assert not any(name.startswith("access-control-") for name in response.headers)
