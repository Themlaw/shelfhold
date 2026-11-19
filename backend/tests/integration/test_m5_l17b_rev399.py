"""Contradicteur of #399 (M5 L17b): what the grey covers of `/lite` and the capability redirect
leave open. Each test is red until its finding is fixed (`xfail(strict=True)`).

R1: the `nomodule` redirect is an inline script allowed by its hash alone. A browser that enforces
CSP Level 1 (an older WebKit, the engine of the e-readers the redirect is for) does not know hash
sources: it blocks the script, and, JavaScript being on, the `<noscript>` refresh does not run
either. The page stays blank. A script file under `'self'` is allowed by every CSP level.

R2: a grey thumbnail has no ledger row. When its cover is no longer referenced and no colour
thumbnail was ever made (a `/lite` reader, before the catch-up), the collection does not drop it:
only the sweep does, a day after it was written, where a colour thumbnail goes at once.
"""

from __future__ import annotations

from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import catalog
from tests.integration.test_thumbnails import Rig
from tests.thumbnail_env import thumbnail_files

pytestmark = pytest.mark.req("EF-BIB-020", "EF-LEC-010")

ROOT = Path(__file__).parents[3]


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[dict[str, str | None], str]] = []
        self._open: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._open = dict(attrs)
            self.found.append((self._open, ""))

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            attrs, body = self.found[-1]
            self.found[-1] = (attrs, body + data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._open = None


def test_rev399_legacy_redirect_runs_under_csp_level_1() -> None:
    parser = _Scripts()
    parser.feed((ROOT / "frontend" / "src" / "app.html").read_text(encoding="utf-8"))
    legacy = [(attrs, body) for attrs, body in parser.found if "nomodule" in attrs]
    assert legacy, "no nomodule redirect"
    for attrs, body in legacy:  # a file of the origin: `'self'` covers it at every CSP level
        assert attrs.get("src") and not body.strip(), (attrs, body)


@pytest.fixture
def rig(settings: Settings) -> Iterator[Rig]:
    made = Rig(settings)
    yield made
    made.engine.dispose()


def test_rev399_grey_thumbnail_goes_with_its_cover(rig: Rig) -> None:
    gone = rig.cover("twelve", (0, 60, 0))
    made = rig.service.get(gone, 160, grey=True)  # what `/lite` asks, before any catch-up
    assert made is not None and not rig.rows()
    assert any("grey" in p.name for p in thumbnail_files(rig.data))
    with rig.engine.begin() as connection:  # the Edition no longer points at its cover
        connection.execute(
            sa.delete(catalog.metadata_field).where(
                catalog.metadata_field.c.entity_id == rig.seed.editions["twelve"]
            )
        )
    rig.service.collect(rig.referenced(), closing=lambda: False)
    left = [p.name for p in thumbnail_files(rig.data) if gone in p.name]
    assert left == [], left
