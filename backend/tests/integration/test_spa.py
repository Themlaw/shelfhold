"""The backend serves the built SPA (ADR-0013); the API keeps priority over the fallback."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.main import create_app

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


@pytest.mark.parametrize("path", ["/..%2fsecret.txt", "/%2e%2e/secret.txt", "/a%00b", "/a%00.txt"])
def test_path_traversal_and_nul_are_index_or_404(
    settings: Settings, build_dir: Path, tmp_path: Path, path: str
) -> None:
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    with _client(settings, build_dir) as client:
        response = client.get(path)
    assert "secret" not in response.text
    assert response.status_code == 404 or response.text == INDEX


def test_symlink_escaping_the_build_is_not_served(
    settings: Settings, build_dir: Path, tmp_path: Path
) -> None:
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    (build_dir / "leak.txt").symlink_to(tmp_path / "secret.txt")
    (build_dir / "leak").symlink_to(tmp_path / "secret.txt")
    with _client(settings, build_dir) as client:
        with_ext = client.get("/leak.txt")
        without_ext = client.get("/leak")
    assert with_ext.status_code == 404
    assert without_ext.text == INDEX


@pytest.mark.parametrize("path", ["/missing.js", "/_app/nope", "/_app/immutable/nope.js", "/x.png"])
def test_missing_file_or_app_path_is_404_not_index(
    settings: Settings, build_dir: Path, path: str
) -> None:
    with _client(settings, build_dir) as client:
        response = client.get(path)
    assert response.status_code == 404
    assert INDEX not in response.text


@pytest.mark.parametrize("path", ["/api", "/api/", "/api/v1/x", "/healthz/x", "/readyz/x"])
def test_api_paths_are_404_without_slash_too(
    settings: Settings, build_dir: Path, path: str
) -> None:
    with _client(settings, build_dir) as client:
        response = client.get(path)
    assert response.status_code == 404
    assert INDEX not in response.text


@pytest.mark.req("ENF-SEC-004", "ENF-PRIV-001")
def test_security_and_cache_headers(settings: Settings, build_dir: Path) -> None:
    with _client(settings, build_dir) as client:
        index = client.get("/")
        fallback = client.get("/library/42")
        asset = client.get("/_app/immutable/chunk.js")
    for response in (index, fallback, asset):
        assert response.headers["x-content-type-options"] == "nosniff"
    assert index.headers["cache-control"] == "no-cache"
    assert fallback.headers["cache-control"] == "no-cache"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert index.headers["content-security-policy"] == f"{CSP}; frame-ancestors 'none'"


def test_head_is_supported(settings: Settings, build_dir: Path) -> None:
    with _client(settings, build_dir) as client:
        index = client.head("/")
        asset = client.head("/_app/immutable/chunk.js")
        missing = client.head("/missing.js")
    assert index.status_code == 200
    assert index.content == b""
    assert index.headers["x-content-type-options"] == "nosniff"
    assert asset.status_code == 200
    assert missing.status_code == 404


def test_without_build_only_the_api_answers(settings: Settings, tmp_path: Path) -> None:
    with _client(settings, tmp_path / "absent") as client:
        assert client.get("/").status_code == 404
        assert client.get("/healthz").status_code == 200


@pytest.mark.req("ENF-OPS-003")
def test_openapi_describes_healthz_response(settings: Settings) -> None:
    with _client(settings, None) as client:
        schema = client.app.openapi()  # type: ignore[attr-defined]
    ok = schema["paths"]["/healthz"]["get"]["responses"]["200"]["content"]["application/json"]
    assert "$ref" in ok["schema"]


@pytest.mark.req("EF-LEC-012")
def test_manifest_and_service_worker_are_served_with_their_own_types(
    settings: Settings, build_dir: Path
) -> None:
    """The manifest has its own media type and the worker is revalidated at each start: a worker
    kept by the HTTP cache would hold a stale shell for ever."""
    (build_dir / "manifest.webmanifest").write_text('{"name": "Shelfhold"}', encoding="utf-8")
    (build_dir / "service-worker.js").write_text(
        "self.addEventListener('fetch', () => {});", "utf-8"
    )
    with _client(settings, build_dir) as client:
        manifest = client.get("/manifest.webmanifest")
        worker = client.get("/service-worker.js")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert worker.headers["content-type"].split(";")[0] in {
        "text/javascript",
        "application/javascript",
    }
    assert worker.headers["cache-control"] == "no-cache"
    assert "service-worker-allowed" not in worker.headers  # the scope stays the origin root
    # its global scope may read its own origin and nothing else, whatever the build holds
    assert worker.headers["content-security-policy"].startswith(
        "default-src 'none'; connect-src 'self'"
    )


@pytest.mark.req("EF-LEC-012")
def test_a_missing_service_worker_is_not_the_shell(settings: Settings, build_dir: Path) -> None:
    """A browser that asks for the worker of a build that has none is not handed an HTML page."""
    with _client(settings, build_dir) as client:
        response = client.get("/service-worker.js")
    assert response.status_code == 404


ODD_PATHS = [
    "/lire%2F..",
    "/lire%2f..%2fx",
    "/lire%2F%2e%2e",
    "/lire/%2e%2e/x",
    "/lire/%2E%2E",
    "/bibliotheque%2Fx",
    "//lire",
    "//lire/U1",
    "/a//b",
    "/content%2F..%2Fx",
    "/content/%2e%2e/api",
    "/api%2F..",
    "/api//v1/me",
    "/_app%2Fimmutable%2Fchunk.js",
]


@pytest.mark.req("EF-LEC-012", "ENF-SEC-004")
@pytest.mark.parametrize("path", ODD_PATHS)
def test_odd_request_line_is_never_served_as_a_page(
    settings: Settings, build_dir: Path, path: str
) -> None:
    """One rule with the worker (`isReserved`): a request line whose decoded form holds a dot
    segment, an encoded slash or a double slash is no page, for any prefix: a neutral 404, neither
    the reader nor the shell nor a file."""
    (build_dir / "reader-entry").mkdir()
    (build_dir / "reader-entry" / "reader.html").write_text(
        "<!doctype html><script>boot()</script><div id=reader></div>", encoding="utf-8"
    )
    with _client(settings, build_dir) as client:
        # absolute, or the client reads a leading `//` as the host of a URL
        response = client.get(f"http://testserver{path}")
    assert response.status_code in {404, 405}
    assert "<title>Shelfhold</title>" not in response.text
    assert "id=reader" not in response.text


@pytest.mark.req("EF-LEC-012")
@pytest.mark.parametrize("path", ["/", "/bibliotheque", "/bibliotheque/", "/serie/abc", "/lire/U1"])
def test_plain_request_line_is_still_served(settings: Settings, build_dir: Path, path: str) -> None:
    (build_dir / "reader-entry").mkdir()
    (build_dir / "reader-entry" / "reader.html").write_text(
        "<!doctype html><script>boot()</script>", encoding="utf-8"
    )
    with _client(settings, build_dir) as client:
        assert client.get(path).status_code == 200
