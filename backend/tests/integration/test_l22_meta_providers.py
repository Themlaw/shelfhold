"""M4 L22 (EF-BIB-013, ENF-LEG-005): the optional ComicVine provider through the host.

Off by default: non-commercial, one key per instance, a link back is shown, `terms_url` stated.
Off means no request at all; on without a key means no request either. A fabricated server answers;
nothing leaves the machine. No route and no screen serves it (`meta enrich` is the local
operator's command), so the child, member, API key and IDOR attacks have no surface here.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.catalog.application.enrich import EnrichReport, EnrichService
from shelfhold.config import Settings, load_settings
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.imaging import WorkerLimits
from shelfhold.infrastructure.schema import metadata
from shelfhold.plugin_host.adapters.native.comicvine import ComicVineProvider, build_manifest
from shelfhold.sdk.metadata import MatchHints
from tests.fakes.cassette_server import CassetteServer
from tests.fakes.comicvine.server import ABSENT, TITLE, VOLUME_URL, fake_comicvine
from tests.fakes_clock import ManualClock
from tests.integration.test_cli_meta_enrich import field
from tests.integration.test_cli_meta_providers import wire
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.l21b_bench import FakeTime, run, worker
from tests.l54_env import bench54

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
LIMITS = WorkerLimits(network_namespace=False)


class Channels:
    def channel_names(self, connection: sa.Connection) -> list[str]:
        return ["ntfy"]


class Bench:
    def __init__(self, engine: Engine, data_dir: Path) -> None:
        self.engine = engine
        self.time = FakeTime()
        clock = ManualClock(NOW)
        self.service = EnrichService(
            engine, clock, EventBus(clock, channels=Channels()), data_dir=data_dir, limits=LIMITS
        )

    def edition(self) -> str:
        with self.engine.begin() as c:
            return add_edition(c, add_work(c))

    def comicvine(
        self, edition: str, title: str, key: str, server: CassetteServer | None = None
    ) -> EnrichReport:
        server = server or fake_comicvine(clock=self.time.now)
        fetcher = worker(build_manifest(), server, self.time)
        return run(
            self.service.enrich(
                ComicVineProvider(api_key=key), fetcher, edition_id=edition,
                hints=MatchHints(title=title),
            )
        )  # fmt: skip

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(sa.text(sql), params).mappings()]

    def dump(self) -> str:
        """Every row of every table, as text: the key must be in none of them."""
        with self.engine.connect() as c:
            lines = []
            for name in sa.inspect(c).get_table_names():
                query = sa.select(sa.table(name)).with_only_columns(sa.text("*"))
                lines += [repr(row) for row in c.execute(query.select_from(sa.table(name)))]
        return "\n".join(lines)


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    engine = create_db_engine(tmp_path / "catalog.db")
    metadata.create_all(engine)
    yield Bench(engine, tmp_path / "data")
    engine.dispose()


def switch_on(monkeypatch: pytest.MonkeyPatch, key: str | None = None) -> None:
    """What the operator does: the two variables of the instance."""
    monkeypatch.setenv("SHELFHOLD_COMICVINE_ENABLED", "1")
    if key is not None:
        monkeypatch.setenv("SHELFHOLD_COMICVINE_API_KEY", key)


@pytest.fixture
def key() -> str:
    return secrets.token_hex(16)  # made up for the test


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    # A fresh instance has it off and no key; asking for it is refused, and not one request leaves.
    assert Settings.model_fields["comicvine_enabled"].default is False
    assert Settings.model_fields["comicvine_api_key"].default is None
    assert settings.comicvine_enabled is False and settings.comicvine_api_key is None
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = fake_comicvine()
        edition = wire(b, fake, monkeypatch)
        capsys.readouterr()
        assert b.cli("meta", "enrich", edition, "--provider", "comicvine", "--title", TITLE) != 0
        err = capsys.readouterr().err
        assert "SHELFHOLD_COMICVINE_ENABLED" in err and "non-commercial" in err.lower()
        assert fake.calls() == []  # nothing asked
        assert field(b, edition, "title") is None


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_enabled_without_a_key_asks_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    switch_on(monkeypatch)
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = fake_comicvine()
        edition = wire(b, fake, monkeypatch)
        capsys.readouterr()
        assert b.cli("meta", "enrich", edition, "--provider", "comicvine", "--title", TITLE) != 0
        assert "SHELFHOLD_COMICVINE_API_KEY" in capsys.readouterr().err
        assert fake.calls() == []


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_enrich_by_cli_prints_the_reminder_and_the_link_back(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    key: str,
) -> None:
    switch_on(monkeypatch, key)
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = fake_comicvine()
        edition = wire(b, fake, monkeypatch)
        capsys.readouterr()
        with structlog.testing.capture_logs() as events:
            args = ("meta", "enrich", edition, "--provider", "comicvine", "--title", TITLE)
            assert b.cli(*args) == 0
        shown = capsys.readouterr()
        out = shown.out
        assert VOLUME_URL in out  # the link back, to the volume's page
        assert "ComicVine" in out and "non-commercial" in out.lower()
        assert "https://comicvine.gamespot.com/api/documentation" in out  # terms_url
        assert json.loads(field(b, edition, "title") or "null") == TITLE
        assert field(b, edition, "published_on")
        assert [c.host for c in fake.calls()] == ["comicvine.gamespot.com"]
        assert key not in out and key not in shown.err and key not in repr(events)
        assert key not in b.rows("SELECT group_concat(value) AS v FROM metadata_field")[0]["v"]


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_enrich_by_cli_needs_a_title_and_names_its_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    key: str,
) -> None:
    switch_on(monkeypatch, key)
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = fake_comicvine()
        edition = wire(b, fake, monkeypatch)
        assert b.cli("meta", "enrich", edition, "--provider", "comicvine") != 0
        assert fake.calls() == []
        capsys.readouterr()
        for status, word in ((401, "unauthorized"), (429, "rate_limited"), (500, "http_503")):
            fake.status = status
            args = ("meta", "enrich", edition, "--provider", "comicvine", "--title", TITLE)
            assert b.cli(*args) == 1
            err = capsys.readouterr().err
            assert "comicvine" in err and word in err and key not in err
        assert field(b, edition, "title") is None


@pytest.mark.req("ENF-SEC-005", "ENF-LEG-005")
def test_comicvine_key_is_never_in_the_settings_dump_or_the_diagnostic(key: str) -> None:

    from shelfhold.ops.diagnostic import SETTING_CLASSES, reduce_config

    env = {"SHELFHOLD_COMICVINE_ENABLED": "1", "SHELFHOLD_COMICVINE_API_KEY": key}
    resolved = load_settings(env)
    settings = resolved.values
    assert settings.comicvine_enabled is True
    assert settings.comicvine_api_key is not None
    assert settings.comicvine_api_key.get_secret_value() == key
    for shown in (repr(settings), str(settings), json.dumps(settings.model_dump(mode="json"))):
        assert key not in shown
    assert SETTING_CLASSES["comicvine_api_key"] == "presence"  # "set", never the value
    assert SETTING_CLASSES["comicvine_enabled"] == "as_is"
    assert key not in json.dumps(reduce_config(resolved), default=str)


@pytest.mark.req("ENF-SEC-005")
def test_comicvine_key_is_in_no_startup_log(key: str) -> None:
    from shelfhold.main import SECRET_SETTINGS, log_effective_config

    assert "comicvine_api_key" in SECRET_SETTINGS
    settings = load_settings(
        {"SHELFHOLD_COMICVINE_ENABLED": "1", "SHELFHOLD_COMICVINE_API_KEY": key}
    ).values
    with structlog.testing.capture_logs() as events:
        log_effective_config(settings)
    assert key not in repr(events)
    # switching it on reminds the terms, once, at start-up
    notice = [e for e in events if e["event"] == "startup.comicvine"]
    assert len(notice) == 1
    assert "non-commercial" in notice[0]["notice"].lower()
    assert notice[0]["terms_url"] == "https://comicvine.gamespot.com/api/documentation"


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_backlink_displayed(bench: Bench, key: str) -> None:
    manifest = build_manifest()
    assert manifest.terms_url and manifest.terms_url.startswith("https://")
    assert manifest.homepage == "https://comicvine.gamespot.com"
    assert "non-commercial" in (manifest.attribution or "").lower()
    # What the host shows after an enrichment: the credit and the page of the volume it read.
    edition = bench.edition()
    report = bench.comicvine(edition, TITLE, key)
    assert report.outcome == "enriched" and report.written
    assert "comicvine.gamespot.com" in (report.attribution or "")
    assert "non-commercial" in (report.attribution or "").lower()
    assert report.source_url == VOLUME_URL
    rows = bench.rows(
        "SELECT DISTINCT provider FROM metadata_field WHERE entity_id = :e", e=edition
    )
    assert [r["provider"] for r in rows] == ["comicvine"]  # every value says where it came from


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_never_replaces_a_locked_field(bench: Bench, key: str) -> None:
    from shelfhold.catalog.public import FieldKey, lock_field

    edition = bench.edition()
    with bench.engine.begin() as c:
        lock_field(c, FieldKey("edition", edition, "title"), "Typed by hand.", now=NOW)
    report = bench.comicvine(edition, TITLE, key)
    (kept,) = bench.rows(
        "SELECT value, locked FROM metadata_field WHERE entity_id = :e AND field = 'title'",
        e=edition,
    )
    assert kept["locked"] == 1 and "Typed by hand." in str(kept["value"])
    assert [k.field for k in report.kept_locked] == ["title"] and report.written


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "unauthorized"), (429, "rate_limited"), (500, "http_503")],
)  # fmt: skip
def test_comicvine_down_is_a_notification_and_stores_nothing(
    bench: Bench, key: str, status: int, reason: str
) -> None:
    edition = bench.edition()
    server = fake_comicvine(clock=bench.time.now, status=status)
    report = bench.comicvine(edition, TITLE, key, server)
    assert (report.outcome, report.reason) == ("provider_down", reason)
    assert report.written == () and key not in repr(report)
    assert bench.rows("SELECT * FROM metadata_field WHERE entity_id = :e", e=edition) == []
    assert key not in bench.dump()  # not in a row, not in a notification, not in the cache


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
@pytest.mark.parametrize("cassette", ["bad_key", "over_limit", "malformed", "not_json"])
def test_comicvine_a_bad_answer_stores_nothing(bench: Bench, key: str, cassette: str) -> None:
    edition = bench.edition()
    server = fake_comicvine(cassette, clock=bench.time.now)
    report = bench.comicvine(edition, TITLE, key, server)
    assert report.outcome == "provider_down" and report.written == ()
    assert report.reason in {"unauthorized", "rate_limited", "schema_mismatch"}
    assert bench.rows("SELECT * FROM metadata_field WHERE entity_id = :e", e=edition) == []
    assert key not in bench.dump()


@pytest.mark.req("EF-BIB-013")
def test_comicvine_not_found_is_not_found(bench: Bench, key: str) -> None:
    edition = bench.edition()
    assert bench.comicvine(edition, ABSENT, key).outcome == "not_found"
    assert key not in bench.dump()


@pytest.mark.req("ENF-LEG-005", "ENF-SEC-005")
def test_comicvine_key_is_in_no_row_after_a_good_answer(bench: Bench, key: str) -> None:
    edition = bench.edition()
    server = fake_comicvine(clock=bench.time.now)
    assert bench.comicvine(edition, TITLE, key, server).outcome == "enriched"
    assert bench.comicvine(edition, TITLE, key, server).outcome == "enriched"
    assert len(server.calls()) == 2  # a request that carries a key is not filed in a cache
    assert key not in bench.dump()
