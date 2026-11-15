"""M4 L20 (EF-BIB-013, ENF-LEG-005): MangaUpdates, its attribution, its release calendar.

The calendar is cached in the database (a second reading costs no request), is read by
`series.stale` without any network, and never shows a child a title her ceiling hides. Proposals
never replace a locked or hand-typed field. A fabricated cassette answers; nothing leaves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.acquisition.application.staleness import stale_series
from shelfhold.catalog.application.enrich import EnrichService
from shelfhold.catalog.application.release_calendar import CALENDAR_TTL, ReleaseCalendar
from shelfhold.catalog.public import FieldKey, lock_field
from shelfhold.events.bus import EventBus
from shelfhold.identity.domain.visibility import ALL, NOBODY, ViewerScope
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.imaging import WorkerLimits
from shelfhold.infrastructure.schema import acquisition, catalog, metadata
from shelfhold.plugin_host.adapters.native.mangaupdates import (
    MangaUpdatesProvider,
    build_manifest,
)
from shelfhold.plugin_host.provider_fetcher import ProviderFetcher, ProviderRuntime, open_provider
from shelfhold.sdk.metadata import MatchHints
from tests.fakes.mangaupdates.server import TITLE, fake_mangaupdates
from tests.fakes_clock import ManualClock
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
LIMITS = WorkerLimits(network_namespace=False)
CHILD = ViewerScope("ceiling", 12)


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


class Channels:
    def channel_names(self, connection: sa.Connection) -> list[str]:
        return ["ntfy"]


class Bench:
    def __init__(self, engine: Engine, data_dir: Path) -> None:
        self.engine = engine
        self.clock = ManualClock(NOW)
        self.server = fake_mangaupdates()
        self.t = 20_000.0
        self.runtime = ProviderRuntime(clock=lambda: self.t, sleep=self._sleep, wall=lambda: self.t)
        bus = EventBus(self.clock, channels=Channels())
        self.service = EnrichService(engine, self.clock, bus, data_dir=data_dir, limits=LIMITS)
        self.calendar = ReleaseCalendar(engine, self.clock)

    async def _sleep(self, seconds: float) -> None:
        self.t += seconds

    def fetcher(self) -> ProviderFetcher:
        self.server.clock = lambda: self.t
        return open_provider(
            build_manifest(), self.server.resolver, contact=None, runtime=self.runtime,
            transport=self.server.transport,
        )  # fmt: skip

    def edition(
        self, *, age: int | None = 0, followed: bool = False, seen_days_ago: int = 17
    ) -> str:
        """An Edition of a Work rated `age`; a followed one has a source and one discovery."""
        with self.engine.begin() as c:
            rated = {"rated_at": NOW} if age is not None else {}
            work = add_work(c, age_rating=age, **rated)
            edition = add_edition(c, work, **rated)
            if followed:
                item = add_source_item(c, remote_id=f"series-{edition}")
                binding = add_binding(c, work, edition, item)
                variant = add_variant(c, add_unit(c, edition), binding)
                seen = NOW - timedelta(days=seen_days_ago)
                v = catalog.unit_variant
                c.execute(sa.update(v).where(v.c.id == variant).values(first_seen_at=seen))
                follow = acquisition.series_follow
                c.execute(sa.insert(follow).values(edition_id=edition, work_id=work))
        return edition

    def refresh(self, edition: str) -> Any:
        return run(self.calendar.refresh(MangaUpdatesProvider(), self.fetcher(),
                                         edition_id=edition, title=TITLE))  # fmt: skip

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(sa.text(sql), params).mappings()]


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    engine = create_db_engine(tmp_path / "catalog.db")
    metadata.create_all(engine)
    yield Bench(engine, tmp_path / "data")
    engine.dispose()


# -- attribution -------------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_mangaupdates_attribution_displayed(bench: Bench) -> None:
    manifest = build_manifest()
    assert "MangaUpdates" in (manifest.attribution or "") and manifest.terms_url
    edition = bench.edition()
    report = run(bench.service.enrich(MangaUpdatesProvider(), bench.fetcher(), edition_id=edition,
                                      hints=MatchHints(title=TITLE)))  # fmt: skip
    assert report.outcome == "enriched" and report.written
    assert "MangaUpdates" in (report.attribution or "")  # what the CLI and the sheet show
    assert (report.source_url or "").startswith("https://www.mangaupdates.com/series/")
    stored = bench.rows("SELECT provider, provenance FROM metadata_field WHERE entity_id = :e",
                        e=edition)  # fmt: skip
    assert stored and {r["provider"] for r in stored} == {"mangaupdates"}
    bench.refresh(edition)
    (entry,) = bench.calendar.entries(ALL)
    assert "MangaUpdates" in entry.attribution and entry.source_url.startswith("https://")


@pytest.mark.req("EF-BIB-013")
def test_mangaupdates_never_replaces_a_locked_field(bench: Bench) -> None:
    edition = bench.edition()
    with bench.engine.begin() as c:
        lock_field(c, FieldKey("edition", edition, "description"), "Typed by hand.", now=NOW)
    run(bench.service.enrich(MangaUpdatesProvider(), bench.fetcher(), edition_id=edition,
                             hints=MatchHints(title=TITLE)))  # fmt: skip
    (kept,) = bench.rows("SELECT value, locked FROM metadata_field WHERE entity_id = :e "
                         "AND field = 'description'", e=edition)  # fmt: skip
    assert kept["locked"] == 1 and "Typed by hand." in str(kept["value"])


# -- the release calendar ----------------------------------------------------------------------


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_mangaupdates_release_calendar_cached(bench: Bench) -> None:
    edition = bench.edition(followed=True)
    assert bench.refresh(edition).outcome == "fetched"
    spent = len(bench.server.calls())
    assert spent >= 1
    assert bench.refresh(edition).outcome == "cached"  # within the lifetime: no request
    assert len(bench.server.calls()) == spent
    (entry,) = bench.calendar.entries(ALL)
    assert entry.last_release_on == date(2026, 11, 2) and entry.chapter == "61"
    assert len(bench.server.calls()) == spent  # reading costs nothing
    bench.clock.advance(seconds=CALENDAR_TTL.total_seconds() + 1)
    assert bench.refresh(edition).outcome == "fetched"  # expired: asked again
    assert len(bench.server.calls()) > spent


@pytest.mark.req("EF-BIB-013", "EF-SRC-026")
def test_series_stale_reads_the_cached_calendar_without_network(bench: Bench) -> None:
    edition = bench.edition(followed=True, seen_days_ago=17)  # past the 14-day floor
    with bench.engine.connect() as c:
        assert [s.edition_id for s in stale_series(c, NOW)] == [edition]  # no calendar: stale
    bench.refresh(edition)
    spent = len(bench.server.calls())
    with bench.engine.connect() as c:  # upstream released on 2 Nov, after our last discovery
        (found,) = stale_series(c, NOW)
        assert found.upstream_release_on == date(2026, 11, 2)  # the source is behind: still stale
    with bench.engine.begin() as c:  # upstream has nothing newer than what we already have
        c.execute(sa.text("UPDATE provider_release_calendar SET last_release_on = '2026-10-01'"))
    with bench.engine.connect() as c:
        assert stale_series(c, NOW) == []  # silence that comes from upstream is not an alert
    assert len(bench.server.calls()) == spent  # series.stale never calls out


# -- a child's ceiling ------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-013", "EF-USR-006")
def test_release_calendar_never_leaks_a_hidden_title_to_a_child(bench: Bench) -> None:
    open_edition = bench.edition(age=0)
    adult_edition = bench.edition(age=18)
    unrated_edition = bench.edition(age=None)
    for edition in (open_edition, adult_edition, unrated_edition):
        bench.refresh(edition)
    assert {e.edition_id for e in bench.calendar.entries(ALL)} == {
        open_edition, adult_edition, unrated_edition,
    }  # fmt: skip
    assert [e.edition_id for e in bench.calendar.entries(CHILD)] == [open_edition]
    assert bench.calendar.entries(NOBODY) == []
    # an IDOR: asking by the identifier of the title that is hidden is the same as an unknown one
    hidden = bench.calendar.entries(CHILD, edition_id=adult_edition)
    unknown = bench.calendar.entries(CHILD, edition_id="01HZZZZZZZZZZZZZZZZZZZZZZZ")
    assert hidden == unknown == []
    assert bench.calendar.entries(CHILD, edition_id=open_edition) != []
