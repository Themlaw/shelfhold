"""L13 (EF-BIB-013, ENF-LEG-005): `shelfhold meta enrich <isbn|edition>`, the real command over a
real instance with a fake OpenLibrary. The agent carries the contact the instance configured."""

from __future__ import annotations

from pathlib import Path

import pytest

from shelfhold.catalog.application.external_ids import add_external_id
from shelfhold.catalog.domain.external_ids import ExternalId
from shelfhold.config import Settings
from shelfhold.infrastructure.fetcher import Network
from shelfhold.plugin_host import provider_fetcher
from shelfhold.plugin_host.provider_fetcher import ProviderRuntime
from tests.fakes.openlibrary.server import API_HOST, ISBN, FakeOpenLibrary
from tests.l54_env import Bench54, bench54

CONTACT = "owner@example.org"


class Quick:
    t = 100.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def wire(b: Bench54, fake: FakeOpenLibrary, monkeypatch: pytest.MonkeyPatch) -> str:
    from shelfhold import composition

    quick = Quick()
    fake.clock = quick.now
    monkeypatch.setattr(composition, "_DEFAULT_NETWORK",
                        Network(resolver=fake.resolver, transport=fake.transport))  # fmt: skip
    monkeypatch.setattr(provider_fetcher, "DEFAULT_RUNTIME",
                        ProviderRuntime(clock=quick.now, sleep=quick.sleep))  # fmt: skip
    edition = b.env.new_series("Synthetic title").edition_id
    with b.engine.begin() as connection:
        add_external_id(connection, edition, ExternalId("edition", "isbn", ISBN), origin="manual")
    return edition


def field(b: Bench54, edition: str, name: str) -> str | None:
    rows = b.rows("SELECT value FROM metadata_field WHERE entity_id = :e AND field = :f",
                  e=edition, f=name)  # fmt: skip
    return str(rows[0]["value"]) if rows else None


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_cli_meta_enrich(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = FakeOpenLibrary()
        edition = wire(b, fake, monkeypatch)
        capsys.readouterr()
        assert b.cli("meta", "enrich", ISBN) == 0
        out = capsys.readouterr().out
        assert "Open Library" in out and "contributors" in out
        assert field(b, edition, "contributors") == '["Ada Example","Bo Sample"]'
        assert field(b, edition, "cover") is not None
        agents = {s.user_agent for s in fake.seen}
        assert len(agents) == 1 and "@" not in str(agents)  # no contact configured: none sent
        assert b.cli("meta", "enrich", edition) == 0  # by Edition: the same book


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_cli_meta_enrich_sends_the_configured_contact(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHELFHOLD_METADATA_CONTACT", CONTACT)
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = FakeOpenLibrary()
        wire(b, fake, monkeypatch)
        assert b.cli("meta", "enrich", ISBN) == 0
        assert {s.user_agent for s in fake.seen if s.host == API_HOST} != set()
        assert all(CONTACT in str(s.user_agent) for s in fake.seen)


@pytest.mark.req("EF-BIB-013", "ENF-OPS-011")
def test_cli_meta_enrich_refuses_what_it_cannot_do(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = FakeOpenLibrary()
        edition = wire(b, fake, monkeypatch)
        capsys.readouterr()
        assert b.cli("meta", "enrich", "9780306406164") != 0  # no such book here
        assert "Refusé" in capsys.readouterr().err
        fake.status = 503
        assert b.cli("meta", "enrich", ISBN) != 0
        err = capsys.readouterr().err
        assert "503" in err or "indisponible" in err
        assert field(b, edition, "title") is None
        rows = b.rows("SELECT type FROM event WHERE type = 'metadata.provider_down'")
        assert len(rows) == 1


@pytest.mark.req("EF-BIB-013", "ENF-LEG-007")
def test_default_runtime_is_a_single_object() -> None:
    assert provider_fetcher.default_runtime() is provider_fetcher.default_runtime()
