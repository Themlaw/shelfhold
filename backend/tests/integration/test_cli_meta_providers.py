"""M4 L20 (EF-BIB-013, ENF-LEG-005): `shelfhold meta enrich --provider` and `meta calendar`, the
real commands over a real instance with a fabricated MangaUpdates. The credit is printed."""

from __future__ import annotations

from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.fetcher import Network
from shelfhold.plugin_host import provider_fetcher
from shelfhold.plugin_host.provider_fetcher import ProviderRuntime
from tests.fakes.cassette_server import CassetteServer
from tests.fakes.mangaupdates.server import TITLE, fake_mangaupdates
from tests.integration.test_cli_meta_enrich import Quick, field
from tests.l54_env import Bench54, bench54


def wire(b: Bench54, fake: CassetteServer, monkeypatch: pytest.MonkeyPatch) -> str:
    from shelfhold import composition

    quick = Quick()
    fake.clock = quick.now
    monkeypatch.setattr(composition, "_DEFAULT_NETWORK",
                        Network(resolver=fake.resolver, transport=fake.transport))  # fmt: skip
    monkeypatch.setattr(provider_fetcher, "DEFAULT_RUNTIME",
                        ProviderRuntime(clock=quick.now, sleep=quick.sleep))  # fmt: skip
    return b.env.new_series(TITLE).edition_id


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_cli_meta_enrich_by_title_keeps_the_calendar_and_prints_the_credit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = fake_mangaupdates()
        edition = wire(b, fake, monkeypatch)
        capsys.readouterr()
        args = ("meta", "enrich", edition, "--provider", "mangaupdates", "--title", TITLE)
        assert b.cli(*args) == 0
        out = capsys.readouterr().out
        assert "MangaUpdates" in out and "https://www.mangaupdates.com/series/700001" in out
        assert field(b, edition, "published_on") is not None
        spent = len(fake.calls())
        assert b.cli("meta", "calendar") == 0  # reading the calendar asks the network nothing
        shown = capsys.readouterr().out
        assert "2026-11-02" in shown and "MangaUpdates" in shown
        assert len(fake.calls()) == spent
        assert b.cli(*args) == 0  # a second time: the calendar is cached, not asked again
        releases = [c for c in fake.calls() if c.path.endswith("/releases/search")]
        assert len(releases) == 3  # two enrichments and one calendar; the second calendar: none


@pytest.mark.req("EF-BIB-013", "ENF-OPS-011")
def test_cli_meta_enrich_by_title_refuses_without_a_title(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = fake_mangaupdates()
        edition = wire(b, fake, monkeypatch)
        assert b.cli("meta", "enrich", edition, "--provider", "anilist") != 0
        assert b.cli("meta", "enrich", edition, "--provider", "mangaupdates", "--title", " ") != 0
        assert fake.calls() == []
