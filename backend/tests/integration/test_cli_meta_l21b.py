"""M4 L21b (EF-BIB-013, EF-BIB-003, ENF-LEG-005): `shelfhold meta enrich --provider bnf|wikidata`,
the real command over a real instance with a fabricated BnF and Wikidata. The credit is printed;
a correspondence is told, never recorded."""

from __future__ import annotations

from pathlib import Path

import pytest

from shelfhold.config import Settings
from tests.fakes.bnf.server import TITLE as BNF_TITLE
from tests.fakes.bnf.server import fake_bnf
from tests.fakes.wikidata.server import QID, fake_wikidata
from tests.fakes.wikidata.server import TITLE as WIKIDATA_TITLE
from tests.integration.test_cli_meta_enrich import field
from tests.integration.test_cli_meta_providers import wire
from tests.l54_env import bench54


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_cli_meta_enrich_bnf_prints_the_source_the_licence_and_records_the_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = fake_bnf()
        edition = wire(b, fake, monkeypatch)
        capsys.readouterr()
        assert b.cli("meta", "enrich", edition, "--provider", "bnf", "--title", BNF_TITLE) == 0
        out = capsys.readouterr().out
        assert "BnF" in out and "Licence Ouverte" in out
        assert "https://catalogue.bnf.fr/ark:/12148/cb00000001a" in out
        assert field(b, edition, "published_on") is not None
        assert "dewey" not in out.lower() and "gallica" not in out.lower()


@pytest.mark.req("EF-BIB-003", "EF-BIB-013")
def test_cli_meta_enrich_wikidata_tells_the_suggestion_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = fake_wikidata()
        edition = wire(b, fake, monkeypatch)
        capsys.readouterr()
        args = ("meta", "enrich", edition, "--provider", "wikidata", "--title", WIKIDATA_TITLE)
        assert b.cli(*args) == 0
        out = capsys.readouterr().out
        assert f"https://www.wikidata.org/entity/{QID}" in out
        assert "viaf" in out and "100000001" in out
        assert "Wikidata" in out  # the credit it asks for
        for name in ("title", "description", "subjects", "published_on"):
            assert field(b, edition, name) is None  # nothing was recorded


@pytest.mark.req("EF-BIB-013", "ENF-OPS-011")
@pytest.mark.parametrize("provider", ["bnf", "wikidata"])
def test_cli_meta_enrich_refuses_a_lookup_by_title_without_a_title(
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = fake_bnf() if provider == "bnf" else fake_wikidata()
        edition = wire(b, fake, monkeypatch)
        assert b.cli("meta", "enrich", edition, "--provider", provider) != 0
        assert b.cli("meta", "enrich", edition, "--provider", provider, "--title", " ") != 0
        assert fake.calls() == []
