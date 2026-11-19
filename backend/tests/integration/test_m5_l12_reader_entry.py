"""M5 L12 (EF-LEC-005, ENF-SEC-004, volet lecteur): `/lire` is its own HTML entry with its own CSP.

The reader is not a route of the SPA: the SPA's policy (`style-src 'self'`, `img-src 'self'`) is not
the reader's, and a document `blob:` inherits the policy of the page that made it (ADR-0055). The
server writes the reader's header itself, from the table of the ADR, after reading the page that
the build produced; a build that does not match is refused at start-up, as `spa_csp` does.
"""

import base64
import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.http_headers import reader_csp
from shelfhold.infrastructure.migrate import migrate
from shelfhold.interfaces.http_api.spa import mount_spa
from shelfhold.main import create_app

BOOT = "import('/reader-entry/assets/main.js');"
SPA_BOOT = "document.documentElement.dataset.theme='bibliotheque';"
SPA_CSP = (
    "default-src 'self'; script-src 'self' 'sha256-AAAA'; style-src 'self'; img-src 'self'; "
    "base-uri 'none'; form-action 'self'; object-src 'none'"
)
ITEM = "01HZZZZZZZZZZZZZZZZZZZZZZA"


def sha(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def _reader(
    scripts: tuple[str, ...] = (BOOT,), head: str = "", *, body: str = "<div id=app></div>"
) -> str:
    inline = "".join(f"<script>{s}</script>" for s in scripts)
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        f'<link rel="stylesheet" href="/reader-entry/assets/main.css">{head}{inline}'
        f"</head><body>{body}</body></html>"
    )


def _spa() -> str:
    from tests.integration.test_spa_csp import _csp, _page

    return _page(_csp(script=sha(SPA_BOOT).strip("'")) + "; img-src 'self'")


def _build(tmp_path: Path, reader: str | None = None) -> Path:
    build = tmp_path / "build"
    (build / "_app" / "immutable").mkdir(parents=True)
    (build / "_app" / "immutable" / "chunk.js").write_text("export {};", encoding="utf-8")
    (build / "index.html").write_text(_spa(), encoding="utf-8")
    if reader is not None:
        assets = build / "reader-entry" / "assets"
        assets.mkdir(parents=True)
        (build / "reader-entry" / "reader.html").write_text(reader, encoding="utf-8")
        (assets / "main.js").write_text("export {};", encoding="utf-8")
        (assets / "main.css").write_text("body{}", encoding="utf-8")
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


@pytest.mark.req("ENF-SEC-004")
def test_reader_csp_font_media_frame_blob() -> None:
    """Directive by directive, the table of ADR-0055."""
    csp = reader_csp(_reader())
    expected = {
        "default-src": ["'none'"],
        "script-src": [sha(BOOT), "'strict-dynamic'"],
        "style-src": ["'self'", "blob:", "'unsafe-inline'"],
        "style-src-elem": ["'self'", "blob:", "'unsafe-inline'"],
        "style-src-attr": ["'unsafe-inline'"],
        "img-src": ["'self'", "blob:"],
        "font-src": ["'self'", "blob:"],
        "media-src": ["'none'"],
        "frame-src": ["blob:"],
        "worker-src": ["'none'"],
        "manifest-src": ["'none'"],
        "connect-src": ["'self'"],
        "object-src": ["'none'"],
        "base-uri": ["'none'"],
        "form-action": ["'none'"],
        "frame-ancestors": ["'self'"],
    }
    for name, values in expected.items():
        assert _directive(csp, name) == values, name
    parts = [p.split()[0] for p in csp.split(";") if p.strip()]
    assert sorted(parts) == sorted(expected), "no directive beyond the table"
    script = _directive(csp, "script-src")
    for forbidden in ("'self'", "blob:", "'unsafe-inline'", "'unsafe-eval'", "*", "data:"):
        assert forbidden not in script
    for name in ("img-src", "font-src", "media-src"):
        assert "data:" not in _directive(csp, name) and "*" not in _directive(csp, name)


@pytest.mark.req("ENF-SEC-004")
def test_reader_csp_hash_follows_the_bytes_of_the_boot_script() -> None:
    other = "import('/reader-entry/assets/other.js');"
    assert sha(other) in reader_csp(_reader((other,)))
    assert sha(BOOT) not in reader_csp(_reader((other,)))


@pytest.mark.req("ENF-SEC-004")
@pytest.mark.parametrize(
    ("page", "why"),
    [
        (_reader(head=f'<meta http-equiv="Content-Security-Policy" content="{SPA_CSP}">'), "meta"),
        (
            _reader(head='<META HTTP-EQUIV="content-security-policy" content="default-src *">'),
            "meta",
        ),
        (_reader((BOOT, "alert(1)")), "inline"),
        ("<!doctype html><head></head><body></body>", "inline"),
        (_reader(head='<script src="/reader-entry/assets/x.js"></script>'), "script"),
        (_reader(head='<link rel="modulepreload" href="/reader-entry/assets/x.js">'), "preload"),
        (_reader(head='<link rel="prefetch" href="/x">'), "link"),
        (_reader(head='<link rel="stylesheet" href="https://cdn.example/x.css">'), "link"),
        (_reader(body='<img src="https://x.example/p.png">'), "remote"),
        (_reader(body='<div onclick="x()"></div>'), "handler"),
    ],
)
def test_a_reader_page_that_is_not_the_audited_one_is_refused(page: str, why: str) -> None:
    """A `<meta>` CSP would stack on the header (the SPA's `style-src 'self'` would win); a second
    inline script, a script file, a preload (refused under `'strict-dynamic'`) or a remote link is
    a page that was not audited."""
    with pytest.raises(RuntimeError, match="reader"):
        reader_csp(page)


@pytest.mark.req("ENF-SEC-004")
def test_reader_entry_has_no_spa_meta_csp(settings: Settings, tmp_path: Path) -> None:
    build = _build(tmp_path, _reader())
    with _client(settings, build) as client:
        html = client.get(f"/lire/unit/{ITEM}").text
    assert "http-equiv" not in html.lower()
    assert "<script>" in html and html.lower().count("<script") == 1
    assert "modulepreload" not in html


@pytest.mark.req("EF-LEC-005")
def test_the_reader_is_served_under_its_own_policy_and_the_spa_keeps_its_own(
    settings: Settings, tmp_path: Path
) -> None:
    build = _build(tmp_path, _reader())
    with _client(settings, build) as client:
        reader = client.get(f"/lire/unit/{ITEM}?scope=excerpt")
        bare = client.get("/lire")
        spa = client.get("/bibliotheque")
    assert reader.status_code == 200 and bare.status_code == 200
    assert reader.headers["content-security-policy"] == bare.headers["content-security-policy"]
    csp = reader.headers["content-security-policy"]
    assert _directive(csp, "script-src") == [sha(BOOT), "'strict-dynamic'"]
    assert _directive(csp, "frame-ancestors") == ["'self'"]
    assert _directive(spa.headers["content-security-policy"], "frame-ancestors") == ["'none'"]
    assert reader.headers["referrer-policy"] == "no-referrer"
    assert reader.headers["cache-control"] == "no-cache"
    # the SPA keeps its policy: no widening, and not a single `blob:` for images any more
    spa_csp = spa.headers["content-security-policy"]
    assert _directive(spa_csp, "style-src") == ["'self'"]
    assert "blob:" not in spa_csp
    assert "strict-dynamic" not in spa_csp
    assert spa.text != reader.text


@pytest.mark.req("ENF-SEC-004")
def test_the_assets_of_the_reader_are_immutable_and_unreachable_as_a_shell(
    settings: Settings, tmp_path: Path
) -> None:
    build = _build(tmp_path, _reader())
    with _client(settings, build) as client:
        asset = client.get("/reader-entry/assets/main.js")
        missing = client.get("/reader-entry/assets/nope.js")
        direct = client.get("/reader-entry/reader.html")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
    assert missing.status_code == 404
    # the page of the reader, fetched by its file name, is never served without its policy
    if direct.status_code == 200:
        assert "content-security-policy" in direct.headers


@pytest.mark.req("ENF-SEC-004")
def test_a_build_without_the_reader_entry_never_serves_the_spa_shell_on_lire(
    settings: Settings, tmp_path: Path
) -> None:
    build = _build(tmp_path, None)
    with _client(settings, build) as client:
        response = client.get(f"/lire/unit/{ITEM}")
    assert response.status_code == 404  # the shell would carry the SPA's policy


@pytest.mark.req("ENF-SEC-004")
def test_a_build_with_a_loose_reader_page_is_refused_at_start_up(
    settings: Settings, tmp_path: Path
) -> None:
    build = _build(tmp_path, _reader((BOOT, "fetch('/api/v1/auth/session')")))
    migrate(settings)
    with pytest.raises(RuntimeError, match="reader"):
        create_app(settings.model_copy(update={"frontend_dir": build}))


@pytest.mark.req("ENF-SEC-004")
def test_mount_spa_reads_the_reader_page_once(tmp_path: Path) -> None:
    build = _build(tmp_path, _reader())
    assert mount_spa(FastAPI(), build) is True


@pytest.mark.req("ENF-SEC-004")
@pytest.mark.parametrize(
    ("written", "served"),
    [
        ("img-src 'self'; frame-ancestors 'self'", "img-src 'self'; frame-ancestors 'self'"),
        ("frame-ancestors  'SELF'", "frame-ancestors  'SELF'"),
        ("frame-ancestors 'self' https://x.example", "frame-ancestors 'none'"),
        ("frame-ancestors *", "frame-ancestors 'none'"),
        ("frame-ancestors 'self' *", "frame-ancestors 'none'"),
        ("img-src 'self'", "img-src 'self'; frame-ancestors 'none'"),
    ],
)
def test_the_middleware_lets_through_only_the_exact_reader_frame_ancestors(
    written: str, served: str
) -> None:
    """A route that writes anything else than `frame-ancestors 'self'` gets `'none'`: the opening
    is the reader's alone (a `blob:` document inherits it, WebKit checks it for the frame)."""
    from shelfhold.infrastructure.http_headers import with_frame_ancestors

    assert with_frame_ancestors(written) == served
