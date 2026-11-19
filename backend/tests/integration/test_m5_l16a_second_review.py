"""The second adverse review of L16a (the installable shell): a red proof of the gap that stays
open. An ``xfail(strict=True)`` marks a real gap: it turns green (and the suite red, the strict
mark refusing a quiet pass) the day the gap is closed, and is then rewritten as a plain test;
``--runxfail`` shows it red today. The worker half of the same gap is the ``it.fails`` of
``frontend/src/lib/pwa/second-review.test.ts``."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.main import create_app

CSP = "default-src 'self'; script-src 'self' 'sha256-abc='"
INDEX = (
    f'<!doctype html><meta http-equiv="content-security-policy" content="{CSP}">'
    "<title>shelf</title>"
)
READER = (
    "<!doctype html><html><head><script>boot()</script></head>"
    '<body><div id="reader"></div></body></html>'
)


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    build = tmp_path / "build"
    (build / "reader-entry").mkdir(parents=True)
    (build / "index.html").write_text(INDEX, encoding="utf-8")
    (build / "reader-entry" / "reader.html").write_text(READER, encoding="utf-8")
    return build


def _client(settings: Settings, build: Path) -> TestClient:
    migrate(settings)
    return TestClient(create_app(settings.model_copy(update={"frontend_dir": build})))


@pytest.mark.req("EF-LEC-012", "ENF-SEC-004")
@pytest.mark.xfail(
    strict=True,
    reason="the reader entry is served for a request line whose decoded path leaves the reader"
    " through its own dot segments, while the worker resolves the same dots and keeps the shell"
    " of the SPA for it offline (the shell in place of the reader, which L16a promised never)",
)
@pytest.mark.parametrize("path", ["/lire%2F..", "/lire%2F..%2Fx", "/lire%2F%2e%2e"])
def test_offline_reader_entry_keeps_reader_csp_the_entry_is_not_served_where_it_resolves_out(
    settings: Settings, build_dir: Path, path: str
) -> None:
    """The server decodes the request line before it routes (a percent-encoded slash becomes a
    real one), and it decides "this is the reader" on the raw first segment of that decoded path:
    `/lire%2F..` reaches it as `/lire/..`, and the reader entry is served (measured against the
    app under a real ASGI server: the same three request lines all answer the reader page). The
    worker decodes the same way, but then resolves the dot segments (`isReserved`:
    `..` pops `lire`), so the very same request lines are not reserved to it: offline, its fetch
    handler answers the navigation with the kept shell of the SPA. That is less than the server
    serves as the reader, which the lot claimed never happens, and the shell in place of the
    reader, which it promised never to serve. Closing either layer closes the gap: the worker
    keeps the raw first segment of the decoded path next to the resolved one, or the server
    resolves its dots before it decides the page is the reader."""
    with _client(settings, build_dir) as client:
        response = client.get(path)
    assert READER not in response.text
