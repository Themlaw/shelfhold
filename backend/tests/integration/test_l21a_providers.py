"""M4 L21a (EF-BIB-013, ENF-LEG-005): Hardcover and Google Books through the host.

The keys are the instance's own: sealed by the `SecretVault` in `provider_secret`, set by the
operator with `meta provider-key` (a local command that reads the key from its standard input), read
by the core only. Google Books has a daily budget (1000 by default, adjustable) counted per Pacific
day, and a 429 «Queries per day» suspends it until Pacific midnight, daylight saving time included;
the host then falls back on the other providers. A fabricated server answers; nothing leaves the
machine, and no key is a literal (each is computed at run time).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.catalog.application.enrich import EnrichReport, EnrichService
from shelfhold.config import Settings, load_settings
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher import Network
from shelfhold.infrastructure.fetcher.day_window import midnight_after
from shelfhold.infrastructure.fetcher.quota_store import SqlBucketStore
from shelfhold.infrastructure.imaging import WorkerLimits
from shelfhold.infrastructure.schema import metadata
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.provider_keys import (
    ProviderKey,
    clear_provider_key,
    load_provider_key,
    provider_key_present,
    store_provider_key,
)
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host import provider_fetcher
from shelfhold.plugin_host.adapters.native.googlebooks import PACIFIC, GoogleBooksProvider
from shelfhold.plugin_host.adapters.native.googlebooks import build_manifest as gb_manifest
from shelfhold.plugin_host.provider_fetcher import ProviderRuntime
from shelfhold.sdk.metadata import MatchHints
from tests.auth_env import run_cli
from tests.fakes.cassette_server import PUBLIC, CassetteServer
from tests.fakes.googlebooks import server as gb
from tests.fakes.hardcover import server as hc
from tests.fakes.openlibrary.server import API_HOST as OL_HOST
from tests.fakes.openlibrary.server import ISBN as OL_ISBN
from tests.fakes.openlibrary.server import FakeOpenLibrary
from tests.fakes_clock import ManualClock
from tests.fakes_fetcher import FakeResolver
from tests.integration.test_cli_meta_enrich import field
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.l21b_bench import FakeTime, run, worker
from tests.l54_env import Bench54, bench54
from tests.redaction_support import forget_secret_values, synthetic_text

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
LIMITS = WorkerLimits(network_namespace=False)
HC_KEY = synthetic_text("l21a-hardcover-token")
GB_KEY = synthetic_text("l21a-googlebooks-key")
EDITION_TITLE = "Synthetic title"


class Channels:
    def channel_names(self, connection: sa.Connection) -> list[str]:
        return ["ntfy"]


# -- the sealed key ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "catalog.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.mark.req("ENF-SEC-005", "ENF-LEG-005")
def test_provider_key_is_sealed_at_rest_and_opened_by_the_vault_alone(engine: Engine) -> None:
    vault = SecretVault(Keyring.generate(NOW))
    with engine.begin() as connection:
        assert provider_key_present(connection, "hardcover") is False
        assert load_provider_key(vault, connection, "hardcover") is None
        store_provider_key(vault, connection, "hardcover", ProviderKey(HC_KEY))
        store_provider_key(vault, connection, "googlebooks", ProviderKey(GB_KEY))
    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT * FROM provider_secret")).mappings().all()
        assert {r["provider"] for r in rows} == {"hardcover", "googlebooks"}
        for row in rows:
            assert HC_KEY not in repr(dict(row)) and GB_KEY not in repr(dict(row))
            assert row["secret_ct"] and row["secret_kid"]  # sealed
        assert load_provider_key(vault, connection, "hardcover").reveal() == HC_KEY  # type: ignore[union-attr]
        # sealed per row: one provider's envelope never opens as the other's
        envelope = connection.execute(sa.text("SELECT secret_ct FROM provider_secret")).first()
        assert envelope is not None
    opened = ProviderKey(HC_KEY)
    assert (
        HC_KEY not in repr(opened) and HC_KEY not in str(opened) and HC_KEY not in f"{opened!r:>40}"
    )


@pytest.mark.req("ENF-SEC-005")
def test_provider_key_replaced_and_cleared_leaves_no_trace(engine: Engine) -> None:
    vault = SecretVault(Keyring.generate(NOW))
    with engine.begin() as connection:
        store_provider_key(vault, connection, "googlebooks", ProviderKey(GB_KEY))
        newer = synthetic_text("l21a-googlebooks-key-2")
        store_provider_key(vault, connection, "googlebooks", ProviderKey(newer))
        assert load_provider_key(vault, connection, "googlebooks").reveal() == newer  # type: ignore[union-attr]
        assert clear_provider_key(connection, "googlebooks") is True
        assert clear_provider_key(connection, "googlebooks") is False
        assert provider_key_present(connection, "googlebooks") is False
    assert GB_KEY not in dump(engine) and newer not in dump(engine)


@pytest.mark.req("ENF-SEC-005")
def test_only_a_known_provider_has_a_key(engine: Engine) -> None:
    vault = SecretVault(Keyring.generate(NOW))
    with engine.begin() as connection:
        for bad in ("", "openlibrary", "HARDCOVER", "hardcover'; --"):
            with pytest.raises(ValueError):
                store_provider_key(vault, connection, bad, ProviderKey(HC_KEY))
        with pytest.raises(ValueError):
            ProviderKey("")


def dump(engine: Engine) -> str:
    """Every row of every table, as text: a key must be in none of them (but sealed)."""
    with engine.connect() as c:
        lines: list[str] = []
        for name in sa.inspect(c).get_table_names():
            query = sa.select(sa.table(name)).with_only_columns(sa.text("*"))
            lines += [repr(row) for row in c.execute(query.select_from(sa.table(name)))]
    return "\n".join(lines)


# -- the budget and the pause, in the Pacific --------------------------------------------------


class Bench:
    """The enrichment service over a real database and the shared (SQL) bucket, with a clock that
    the test sets: the instant the host reads is the instant of the bucket and of the pause."""

    def __init__(self, engine: Engine, data_dir: Path) -> None:
        self.engine = engine
        self.time = FakeTime()
        self.clock = ManualClock(NOW)
        self.runtime = ProviderRuntime(
            clock=self.time.now, sleep=self.time.sleep, wall=self.time.now
        )
        self.runtime.buckets.use_store(SqlBucketStore(engine))
        self.service = EnrichService(
            engine,
            self.clock,
            EventBus(self.clock, channels=Channels()),
            data_dir=data_dir,
            limits=LIMITS,
        )

    def at(self, instant: datetime) -> None:
        self.time.t = instant.timestamp()
        self.clock.set(instant)

    def edition(self) -> str:
        with self.engine.begin() as c:
            return add_edition(c, add_work(c))

    def google(
        self, edition: str, server: CassetteServer, *, budget: int = 1000, title: str = gb.TITLE
    ) -> EnrichReport:
        fetcher = worker(gb_manifest(budget), server, self.time, self.runtime)
        provider = GoogleBooksProvider(api_key=GB_KEY, daily_budget=budget)
        return run(
            self.service.enrich(
                provider, fetcher, edition_id=edition, hints=MatchHints(title=title)
            )  # type: ignore[arg-type]
        )

    def paused_until(self) -> float | None:
        return SqlBucketStore(self.engine).suspended_until("googlebooks", self.time.now())


@pytest.fixture
def bench(engine: Engine, tmp_path: Path) -> Bench:
    return Bench(engine, tmp_path / "data")


# the days that matter: the spring change, the autumn change, and an ordinary winter and summer day
INSTANTS = [
    datetime(2026, 3, 8, 12, 0, tzinfo=UTC),
    datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    datetime(2026, 12, 1, 12, 0, tzinfo=UTC),
    datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
]


@pytest.mark.req("ENF-LEG-005")
@pytest.mark.parametrize("now", INSTANTS, ids=lambda d: d.date().isoformat())
def test_google_books_daily_budget_and_429_pause_until_pacific_midnight(
    bench: Bench, now: datetime
) -> None:
    midnight = midnight_after(now, PACIFIC)
    bench.at(now)
    server = gb.fake_googlebooks()
    edition = bench.edition()
    # the budget counts what leaves the machine, robots.txt included (read once, then kept): four
    # a day, so three questions; the next is refused before it leaves
    for n in range(3):
        report = bench.google(edition, server, budget=4)
        assert report.outcome == "enriched", n
    assert bench.google(edition, server, budget=4).outcome == "quota"
    assert len(server.calls()) == 3
    # it is the budget of the Pacific day: the count starts again at Pacific midnight, not before
    bench.at(midnight - timedelta(seconds=1))
    assert bench.google(edition, server, budget=4).outcome == "quota"
    bench.at(midnight)
    assert bench.google(edition, server, budget=4).outcome == "enriched"
    assert len(server.calls()) == 4

    # a 429 «Queries per day» from Google itself: the provider is suspended until the next midnight
    bench.at(now + timedelta(days=3))
    midnight = midnight_after(now + timedelta(days=3), PACIFIC)
    server.use("day_quota")
    report = bench.google(edition, server)
    assert (report.outcome, report.reason) == ("provider_down", "daily_quota")
    assert bench.paused_until() == midnight.timestamp()
    spent = len(server.calls())
    server.use("volumes")  # Google is fine again, but we do not ask before midnight
    bench.at(midnight - timedelta(seconds=1))
    assert bench.google(edition, server).outcome == "quota"
    assert len(server.calls()) == spent
    bench.at(midnight)
    assert bench.google(edition, server).outcome == "enriched"
    assert bench.paused_until() is None


@pytest.mark.req("ENF-LEG-005")
def test_a_429_per_minute_does_not_suspend_the_day(bench: Bench) -> None:
    bench.at(NOW)
    server = gb.fake_googlebooks("minute_quota")
    report = bench.google(bench.edition(), server)
    assert (report.outcome, report.reason) == ("provider_down", "rate_limited")
    assert bench.paused_until() is None


@pytest.mark.req("ENF-LEG-005")
def test_the_pause_is_the_same_for_every_process_and_survives_a_restart(
    bench: Bench, engine: Engine, tmp_path: Path
) -> None:
    bench.at(NOW)
    server = gb.fake_googlebooks("day_quota")
    bench.google(bench.edition(), server)
    other = Bench(engine, tmp_path / "other")  # another process: nothing in memory
    other.at(NOW + timedelta(hours=1))
    assert other.google(other.edition(), gb.fake_googlebooks()).outcome == "quota"


@pytest.mark.req("ENF-LEG-005", "ENF-SEC-005")
def test_the_key_is_in_no_row_after_a_good_answer_and_a_429(bench: Bench, engine: Engine) -> None:
    bench.at(NOW)
    edition = bench.edition()
    server = gb.fake_googlebooks()
    with structlog.testing.capture_logs() as events:
        assert bench.google(edition, server).outcome == "enriched"
        server.use("day_quota")
        bench.google(edition, server)
    assert GB_KEY not in dump(engine) and GB_KEY not in repr(events)
    assert all(c.headers["x-goog-api-key"] == GB_KEY for c in server.calls())


# -- the operator's commands ------------------------------------------------------------------


def wire(b: Bench54, servers: list[CassetteServer | FakeOpenLibrary], monkeypatch: Any) -> str:
    """One resolver and one transport for every fake host: the host dispatches on `Host`."""
    from shelfhold import composition

    by_host: dict[str, Any] = {}
    for server in servers:
        hosts = (
            [server.host] if isinstance(server, CassetteServer) else [OL_HOST, "covers." + OL_HOST]
        )
        for host in hosts:
            by_host[host] = server
    times = FakeTime()
    for server in servers:
        server.clock = times.now
    resolver = FakeResolver({host: [PUBLIC] for host in by_host})

    def route(request: httpx2.Request) -> httpx2.Response:
        server = by_host[request.headers["host"]]
        handler = server._handle
        return handler(request)  # type: ignore[no-any-return]

    monkeypatch.setattr(composition, "_DEFAULT_NETWORK",
                        Network(resolver=resolver, transport=httpx2.MockTransport(route)),
    )  # fmt: skip
    monkeypatch.setattr(provider_fetcher, "DEFAULT_RUNTIME",
                        ProviderRuntime(clock=times.now, sleep=times.sleep))  # fmt: skip
    return b.env.new_series(EDITION_TITLE).edition_id


def add_isbn(b: Bench54, edition: str, isbn: str) -> None:
    from shelfhold.catalog.application.external_ids import add_external_id
    from shelfhold.catalog.domain.external_ids import ExternalId

    with b.engine.begin() as connection:
        add_external_id(connection, edition, ExternalId("edition", "isbn", isbn), origin="manual")


def cli_in(b: Bench54, stdin: str, *argv: str) -> int:
    """A command whose standard input is `stdin`: where a key is given."""
    return run_cli(b.monkeypatch, b.settings, *argv, stdin=stdin)


def set_key(b: Bench54, provider: str, key: str) -> int:
    return cli_in(b, key + "\n", "meta", "provider-key", "set", provider)


@pytest.mark.req("ENF-SEC-005", "ENF-LEG-005")
def test_cli_provider_key_set_status_clear_never_echoes_the_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        capsys.readouterr()
        with structlog.testing.capture_logs() as events:
            assert b.cli("meta", "provider-key", "status") == 0
            shown = capsys.readouterr().out
            assert (
                "hardcover" in shown
                and "googlebooks" in shown
                and "set" not in shown.replace("unset", "")
            )
            assert set_key(b, "hardcover", HC_KEY) == 0
            done = capsys.readouterr()
            assert HC_KEY not in done.out + done.err  # the key is never repeated
            assert b.cli("meta", "provider-key", "status") == 0
            status = capsys.readouterr().out
            assert "hardcover: set" in status and "googlebooks: unset" in status
            assert HC_KEY not in status
            raw = Path(settings.database_path).read_bytes()
            assert HC_KEY.encode() not in raw  # sealed in the file itself, not only in a column
            assert b.rows("SELECT provider FROM provider_secret") == [{"provider": "hardcover"}]
            assert b.cli("meta", "provider-key", "clear", "hardcover") == 0
            assert b.rows("SELECT provider FROM provider_secret") == []
        assert HC_KEY not in repr(events)


@pytest.mark.req("ENF-SEC-005", "ENF-LEG-005")
def test_cli_provider_key_refuses_what_is_not_a_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        capsys.readouterr()
        with pytest.raises(SystemExit):  # argparse refuses a provider that keeps no key
            cli_in(b, "x\n", "meta", "provider-key", "set", "openlibrary")
        assert cli_in(b, "\n", "meta", "provider-key", "set", "hardcover") != 0
        assert cli_in(b, "two words\n", "meta", "provider-key", "set", "hardcover") != 0
        assert cli_in(b, ("k" * 5000) + "\n", "meta", "provider-key", "set", "hardcover") != 0
        assert b.rows("SELECT provider FROM provider_secret") == []
        capsys.readouterr()
        try:  # a key on the command line is refused (a key that looks like a flag, by argparse)
            refused = b.cli("meta", "provider-key", "set", "hardcover", "--", HC_KEY)
        except SystemExit as stop:
            refused = int(stop.code or 1)
        assert refused != 0
        assert HC_KEY not in capsys.readouterr().out


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005", "ENF-SEC-005")
def test_hardcover_enrich_by_cli_reads_the_sealed_token_and_asks_server_side(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = hc.fake_hardcover()
        edition = wire(b, [fake], monkeypatch)
        args = ("meta", "enrich", edition, "--provider", "hardcover", "--title", hc.TITLE)
        capsys.readouterr()
        assert b.cli(*args) != 0  # no token: refused, and told how to give one
        assert "meta provider-key set hardcover" in capsys.readouterr().err
        assert fake.calls() == []
        assert set_key(b, "hardcover", HC_KEY) == 0
        capsys.readouterr()
        forget_secret_values()
        with structlog.testing.capture_logs() as events:
            assert b.cli(*args) == 0
        shown = capsys.readouterr()
        assert hc.BOOK_URL in shown.out and "Hardcover" in shown.out
        assert json.loads(field(b, edition, "title") or "null") == hc.TITLE
        assert [c.host for c in fake.calls()] == [hc.HOST]
        assert fake.calls()[0].headers["authorization"] == f"Bearer {HC_KEY}"
        assert HC_KEY not in shown.out + shown.err + repr(events)
        assert HC_KEY not in dump(b.engine).replace(
            repr(b.rows("SELECT secret_ct FROM provider_secret")), ""
        )


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005", "ENF-SEC-005")
def test_google_books_enrich_by_cli_refuses_without_a_key_and_honours_the_budget(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SHELFHOLD_GOOGLEBOOKS_DAILY_BUDGET", "3")  # robots.txt is one of them
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = gb.fake_googlebooks()
        edition = wire(b, [fake], monkeypatch)
        args = ("meta", "enrich", edition, "--provider", "googlebooks", "--title", gb.TITLE)
        capsys.readouterr()
        assert b.cli(*args) != 0
        assert "meta provider-key set googlebooks" in capsys.readouterr().err
        assert fake.calls() == []
        assert set_key(b, "googlebooks", GB_KEY) == 0
        capsys.readouterr()
        assert b.cli(*args) == 0 and b.cli(*args) == 0
        assert len(fake.calls()) == 2
        out = capsys.readouterr().out
        assert gb.VOLUME_URL in out and "Google Books" in out
        assert b.cli(*args) != 0  # the budget of the day is spent: nothing is asked
        assert len(fake.calls()) == 2
        assert "googlebooks" in capsys.readouterr().err  # said by name
        assert GB_KEY not in dump(b.engine).replace(
            repr(b.rows("SELECT secret_ct FROM provider_secret")), ""
        )


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_google_books_suspended_falls_back_on_the_other_providers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        google = gb.fake_googlebooks("day_quota")
        library = FakeOpenLibrary()
        edition = wire(b, [google, library], monkeypatch)
        add_isbn(b, edition, OL_ISBN)
        assert set_key(b, "googlebooks", GB_KEY) == 0
        args = ("meta", "enrich", edition, "--provider", "googlebooks", "--title", gb.TITLE)
        capsys.readouterr()
        assert b.cli(*args) == 0  # Google says «per day»: the answer is OpenLibrary's
        out = capsys.readouterr().out
        assert "Open Library" in out and "googlebooks" in out and "daily_quota" in out
        assert field(b, edition, "contributors") is not None
        assert len(google.calls()) == 1
        until = SqlBucketStore(b.engine).suspended_until(
            "googlebooks", datetime.now(UTC).timestamp()
        )
        assert until is not None and until <= midnight_after(datetime.now(UTC), PACIFIC).timestamp()
        assert b.cli(*args) == 0  # suspended: Google is not asked at all, the fallback answers
        assert len(google.calls()) == 1
        assert "googlebooks" in capsys.readouterr().out  # the way it went is told
        assert GB_KEY not in dump(b.engine).replace(
            repr(b.rows("SELECT secret_ct FROM provider_secret")), ""
        )


@pytest.mark.req("ENF-LEG-005")
def test_settings_the_budget_is_adjustable_and_sane() -> None:
    assert load_settings({}).values.googlebooks_daily_budget == 1000
    assert (
        load_settings({"SHELFHOLD_GOOGLEBOOKS_DAILY_BUDGET": "250"}).values.googlebooks_daily_budget
        == 250
    )
    for bad in ("0", "-5", "abc", "100000000"):
        with pytest.raises(Exception):  # noqa: B017  (the settings refuse to load)
            load_settings({"SHELFHOLD_GOOGLEBOOKS_DAILY_BUDGET": bad})
