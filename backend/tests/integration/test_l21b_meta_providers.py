"""M4 L21b (EF-BIB-013, EF-BIB-003, ENF-LEG-005): BnF and Wikidata through the host's enrichment.

The BnF gives fields with the source, the date and the licence; it never gives a Dewey notice or
a Gallica cover. Wikidata gives suggestions, kept nowhere: no field, no identifier. What a person
types stays in its literal. A fabricated server answers; nothing leaves the machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.catalog.application.enrich import EnrichReport, EnrichService
from shelfhold.catalog.public import FieldKey, lock_field
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher.payload_cache import METADATA_CACHE_TTL
from shelfhold.infrastructure.imaging import WorkerLimits
from shelfhold.infrastructure.schema import metadata
from shelfhold.plugin_host.adapters.native.bnf import BnFProvider
from shelfhold.plugin_host.adapters.native.bnf import build_manifest as bnf_manifest
from shelfhold.plugin_host.adapters.native.wikidata import WikidataProvider
from shelfhold.plugin_host.adapters.native.wikidata import build_manifest as wikidata_manifest
from shelfhold.plugin_host.bnf_sru import read_sru
from shelfhold.plugin_host.provider_fetcher import ProviderFetcher
from shelfhold.sdk.metadata import MatchHints
from tests.fakes.bnf import server as bnf_server
from tests.fakes.cassette_server import CassetteServer
from tests.fakes.wikidata import server as wikidata_server
from tests.fakes_clock import ManualClock
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.l21b_bench import FakeTime, run, worker
from tests.unit.test_l21b_escape import cleaned, literal_of

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
LIMITS = WorkerLimits(network_namespace=False)
BNF = bnf_server.TITLE
SUGGESTED_QID = wikidata_server.QID


class Channels:
    def channel_names(self, connection: sa.Connection) -> list[str]:
        return ["ntfy"]


class Bench:
    def __init__(self, engine: Engine, data_dir: Path) -> None:
        self.engine = engine
        self.time = FakeTime()
        clock = self.clock = ManualClock(NOW)
        self.service = EnrichService(
            engine, clock, EventBus(clock, channels=Channels()), data_dir=data_dir, limits=LIMITS
        )

    def edition(self) -> str:
        with self.engine.begin() as c:
            return add_edition(c, add_work(c))

    def bnf(
        self, edition: str, title: str | None, server: CassetteServer | None = None
    ) -> EnrichReport:
        server = server or bnf_server.fake_bnf(clock=self.time.now)
        fetcher: ProviderFetcher = worker(bnf_manifest(), server, self.time)
        hints = MatchHints(title=title) if title else None
        return run(
            self.service.enrich(
                BnFProvider(reader=read_sru), fetcher, edition_id=edition, hints=hints
            )
        )

    def wikidata(
        self, edition: str, title: str, server: CassetteServer | None = None
    ) -> EnrichReport:
        server = server or wikidata_server.fake_wikidata(clock=self.time.now)
        fetcher = worker(wikidata_manifest(), server, self.time)
        return run(
            self.service.enrich(
                WikidataProvider(), fetcher, edition_id=edition, hints=MatchHints(title=title)
            )
        )

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(sa.text(sql), params).mappings()]


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    engine = create_db_engine(tmp_path / "catalog.db")
    metadata.create_all(engine)
    yield Bench(engine, tmp_path / "data")
    engine.dispose()


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_bnf_attribution_source_and_retrieval_date(bench: Bench) -> None:
    edition = bench.edition()
    before = datetime.now(UTC) - timedelta(seconds=2)
    report = bench.bnf(edition, BNF)
    assert report.outcome == "enriched" and report.written
    assert "BnF" in (report.attribution or "") and "Licence Ouverte" in (report.attribution or "")
    assert report.source_url == "https://catalogue.bnf.fr/ark:/12148/cb00000001a"  # the notice
    stored = bench.rows(
        "SELECT field, provider, fetched_at FROM metadata_field WHERE entity_id = :e", e=edition
    )
    assert {r["field"] for r in stored} == {
        "title",
        "contributors",
        "publisher",
        "published_on",
        "subjects",
    }
    assert {r["provider"] for r in stored} == {"bnf"}
    for row in stored:  # the date of the retrieval is kept with each value
        fetched = (
            row["fetched_at"]
            if isinstance(row["fetched_at"], datetime)
            else datetime.fromisoformat(str(row["fetched_at"]))
        )
        assert fetched.replace(tzinfo=fetched.tzinfo or UTC) >= before


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_bnf_excludes_dewey_and_gallica_covers(bench: Bench) -> None:
    edition = bench.edition()
    server = bnf_server.fake_bnf(clock=bench.time.now)
    report = bench.bnf(edition, BNF, server)
    stored = bench.rows("SELECT field, value FROM metadata_field WHERE entity_id = :e", e=edition)
    text = " ".join(str(r["value"]) for r in stored).lower()
    assert "dewey" not in text and "843" not in text and "classification" not in text
    assert "cover" not in {r["field"] for r in stored} and report.cover is None
    assert report.cover_rejected is None
    assert {c.host for c in server.calls()} == {"catalogue.bnf.fr"}  # no image, no Gallica
    assert all("gallica" not in (c.path + c.query).lower() for c in server.calls())
    assert "gallica" not in repr(report).lower()


@pytest.mark.req("EF-BIB-003", "EF-BIB-013")
def test_wikidata_proposals_are_suggestions_only(bench: Bench) -> None:
    edition = bench.edition()
    before = bench.rows("SELECT * FROM external_id")
    report = bench.wikidata(edition, wikidata_server.TITLE)
    assert report.outcome == "suggested"  # told to the admin, nothing more
    assert report.written == () and report.kept_locked == () and report.cover is None
    (suggestion,) = report.suggestions
    assert suggestion.url == f"https://www.wikidata.org/entity/{SUGGESTED_QID}"
    assert dict(suggestion.correspondences) == {
        "viaf": "100000001",
        "bnf": "00000001a",
        "openlibrary": "OL1W",
    }
    assert suggestion.provider == "wikidata"
    assert "Wikidata" in (report.attribution or "")
    # nothing is stored: not a field, and above all not an identity
    assert bench.rows("SELECT * FROM metadata_field WHERE entity_id = :e", e=edition) == []
    assert bench.rows("SELECT * FROM external_id") == before
    assert SUGGESTED_QID not in repr(bench.rows("SELECT * FROM external_id"))


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_bnf_and_wikidata_queries_escape_user_input(bench: Bench) -> None:
    hostile = ['a"b\\c', '" } UNION { ?s ?p ?o } #', 'x" or bib.author all "y', "two\nlines*?^"]
    edition = bench.edition()
    for typed in hostile:
        sru = bnf_server.fake_bnf(clock=bench.time.now)
        bench.bnf(edition, typed, sru)
        sent = parse_qs(sru.calls()[0].query)["query"][0]
        head = "bib.title all "
        assert sent.startswith(head)
        inside, rest = literal_of(sent, len(head))
        assert rest == "" and inside == cleaned(typed)  # the whole of what the server reads
        wd = wikidata_server.fake_wikidata(clock=bench.time.now)
        bench.wikidata(edition, typed, wd)
        query = parse_qs(wd.calls()[0].query)["query"][0]
        marker = "VALUES ?label { "  # the label, in the two languages asked
        first, rest = literal_of(query, query.index(marker) + len(marker))
        assert rest.startswith("@en ")
        second, rest = literal_of(rest, len("@en "))
        assert first == second == cleaned(typed) and rest.startswith("@fr }")
        assert query.count("SELECT") == 1 and query.count("SERVICE") == 1  # nothing was added
    assert bench.rows("SELECT * FROM metadata_field WHERE entity_id = :e", e=edition) == []


@pytest.mark.req("EF-BIB-013")
def test_bnf_never_replaces_a_locked_field(bench: Bench) -> None:
    edition = bench.edition()
    with bench.engine.begin() as c:
        lock_field(c, FieldKey("edition", edition, "title"), "Typed by hand.", now=NOW)
    report = bench.bnf(edition, BNF)
    (kept,) = bench.rows(
        "SELECT value, locked FROM metadata_field WHERE entity_id = :e AND field = 'title'",
        e=edition,
    )
    assert kept["locked"] == 1 and "Typed by hand." in str(kept["value"])
    assert [k.field for k in report.kept_locked] == ["title"] and report.written


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
@pytest.mark.parametrize("status", [429, 503])
def test_a_provider_that_is_down_is_a_notification_and_stores_nothing(
    bench: Bench, status: int
) -> None:
    edition = bench.edition()
    for report in (
        bench.bnf(edition, BNF, bnf_server.fake_bnf(clock=bench.time.now, status=status)),
        bench.wikidata(
            edition,
            wikidata_server.TITLE,
            wikidata_server.fake_wikidata(clock=bench.time.now, status=status),
        ),
    ):
        assert (report.outcome, report.reason) == ("provider_down", "http_503")
        assert report.written == () and report.suggestions == ()
    assert bench.rows("SELECT * FROM metadata_field WHERE entity_id = :e", e=edition) == []


@pytest.mark.req("EF-BIB-013", "EF-BIB-003")
def test_nothing_known_or_ambiguous_is_not_found(bench: Bench) -> None:
    edition = bench.edition()
    assert bench.bnf(edition, bnf_server.ABSENT).outcome == "not_found"
    assert bench.wikidata(edition, wikidata_server.ABSENT).outcome == "not_found"
    twin = bench.wikidata(edition, wikidata_server.TWIN)  # two items, one label: no guess
    assert (twin.outcome, twin.suggestions) == ("not_found", ())


@pytest.mark.req("ENF-LEG-005")
def test_the_same_question_is_asked_once_within_the_life_of_the_cache(bench: Bench) -> None:
    edition = bench.edition()
    bnf, wd = (
        bnf_server.fake_bnf(clock=bench.time.now),
        wikidata_server.fake_wikidata(clock=bench.time.now),
    )
    for _ in range(3):
        assert bench.bnf(edition, BNF, bnf).outcome == "enriched"
        assert bench.wikidata(edition, wikidata_server.TITLE, wd).outcome == "suggested"
    assert (len(bnf.calls()), len(wd.calls())) == (
        1,
        1,
    )  # the cache is mandatory: a hit costs nothing
    bench.clock.advance(seconds=METADATA_CACHE_TTL.total_seconds() + 1)
    bench.bnf(edition, BNF, bnf)
    bench.wikidata(edition, wikidata_server.TITLE, wd)
    assert (len(bnf.calls()), len(wd.calls())) == (2, 2)  # expired: asked again
