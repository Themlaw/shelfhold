"""L13 (EF-BIB-013, ENF-LEG-005, ENF-SEC-003): enriching an Edition from OpenLibrary.

What the provider proposes goes to `metadata_field` with its provenance and to `age_suggestion`;
a locked or hand-typed value is never replaced; an age is never applied; the cover is decoded by
the worker, never stored as it came; a provider that is down raises a notification and nothing
else. A fake OpenLibrary answers: nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy import Engine

from shelfhold.catalog.application import enrich as enrich_module
from shelfhold.catalog.application.enrich import (
    DOWN_NOTICE_INTERVAL,
    EnrichService,
    UnknownTargetError,
)
from shelfhold.catalog.application.external_ids import add_external_id
from shelfhold.catalog.domain.external_ids import ExternalId
from shelfhold.catalog.public import FieldKey, lock_field
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher.errors import UserAgentNotIdentifiable
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.imaging import WorkerLimits
from shelfhold.infrastructure.imaging import client as imaging_client
from shelfhold.infrastructure.schema import catalog, metadata
from shelfhold.plugin_host.adapters.native.openlibrary import OpenLibraryProvider, build_manifest
from shelfhold.plugin_host.provider_fetcher import ProviderFetcher, ProviderRuntime, open_provider
from shelfhold.sdk.fetcher import Response
from shelfhold.sdk.metadata import (
    EntityRef,
    FieldProposal,
    IdentifyResult,
    MatchHints,
    RelationProposal,
)
from shelfhold.sdk.testing import FakeFetcher
from tests.fakes.openlibrary.server import (
    API_HOST,
    COVER_HOST,
    ISBN,
    ISBN_OTHER,
    FakeOpenLibrary,
    png,
    record,
)
from tests.fakes_clock import ManualClock
from tests.integration.test_schema_catalog import add_edition, add_work

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
LIMITS = WorkerLimits(network_namespace=False)


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


class NoSleep:
    def __init__(self) -> None:
        self.t = 5_000.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class Channels:
    def channel_names(self, connection: sa.Connection) -> list[str]:
        return ["ntfy"]


class Bench:
    def __init__(self, engine: Engine, data_dir: Path, fake: FakeOpenLibrary) -> None:
        self.engine, self.data_dir, self.fake = engine, data_dir, fake
        self.clock = ManualClock(NOW)
        self.time = NoSleep()
        fake.clock = self.time.now
        self.bus = EventBus(self.clock, channels=Channels())
        self.service = EnrichService(engine, self.clock, self.bus, data_dir=data_dir, limits=LIMITS)
        self.runtime = ProviderRuntime(clock=self.time.now, sleep=self.time.sleep)
        with engine.begin() as connection:
            self.work = add_work(connection)
            self.edition = add_edition(connection, self.work)
            add_external_id(connection, self.edition, ExternalId("edition", "isbn", ISBN),
                            origin="manual")  # fmt: skip

    def fetcher(self, contact: str | None = None) -> ProviderFetcher:
        return open_provider(build_manifest(), self.fake.resolver, contact=contact,
                             runtime=self.runtime, transport=self.fake.transport)  # fmt: skip

    def enrich(self, provider: Any = None, fetcher: Any = None, edition: str | None = None) -> Any:
        return run(
            self.service.enrich(
                provider or OpenLibraryProvider(), fetcher or self.fetcher(),
                edition_id=edition or self.edition,
            )
        )  # fmt: skip

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def field(self, name: str) -> dict[str, Any] | None:
        found = self.rows("SELECT * FROM metadata_field WHERE entity_id = :e AND field = :f",
                          e=self.edition, f=name)  # fmt: skip
        return found[0] if found else None

    def events(self) -> list[str]:
        return [r["type"] for r in self.rows("SELECT type FROM event ORDER BY id")]

    def covers(self) -> list[Path]:
        folder = self.data_dir / "covers"
        return sorted(folder.iterdir()) if folder.is_dir() else []


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    engine = create_db_engine(tmp_path / "catalog.db")
    metadata.create_all(engine)
    yield Bench(engine, tmp_path / "data", FakeOpenLibrary())
    engine.dispose()


@pytest.fixture
def spy_worker(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The operations the decode worker was asked for (the real worker still runs)."""
    calls: list[str] = []
    real = imaging_client.run_worker

    def spy(op: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(op)
        return real(op, *args, **kwargs)

    monkeypatch.setattr(imaging_client, "run_worker", spy)
    return calls


# -- the proposals, with their provenance ------------------------------------------------------


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_openlibrary_isbn_proposes_authors_and_cover_with_provenance(
    bench: Bench, spy_worker: list[str]
) -> None:
    report = bench.enrich()
    assert report.outcome == "enriched"
    contributors = bench.field("contributors")
    assert contributors is not None
    assert (contributors["provider"], contributors["provenance"], contributors["locked"]) == (
        "openlibrary",
        "provider",
        0,
    )
    assert contributors["value"] == '["Ada Example","Bo Sample"]'
    assert contributors["entity_type"] == "edition" and contributors["confidence"] is not None
    for name in ("title", "publisher", "published_on"):
        assert bench.field(name) is not None, name
    cover = bench.field("cover")
    assert cover is not None and cover["provider"] == "openlibrary"
    (stored,) = bench.covers()
    assert cover["value"] == f'"covers/{stored.name}"'
    assert "Open Library" in report.attribution and report.source_url
    assert spy_worker, "the cover went through the decode worker"


@pytest.mark.req("EF-BIB-013", "ENF-SEC-003")
def test_the_cover_is_decoded_by_the_worker_and_stored_as_the_worker_made_it(
    bench: Bench, spy_worker: list[str]
) -> None:
    original = png((300, 450))
    bench.fake.covers[ISBN] = original
    bench.enrich()
    assert "thumbnail" in spy_worker
    (stored,) = bench.covers()
    data = stored.read_bytes()
    assert data != original and data[:4] == b"RIFF" and data[8:12] == b"WEBP"  # re-encoded
    assert original not in data
    assert Image.open(io.BytesIO(data)).size[0] <= 1200


@pytest.mark.req("EF-BIB-013", "ENF-SEC-003")
@pytest.mark.parametrize("body", ["bomb", "html", "svg", "empty"])
def test_a_cover_the_worker_refuses_is_never_stored(bench: Bench, body: str) -> None:
    bench.fake.covers[ISBN] = {
        "bomb": png((7_000, 7_000)),  # 49 Mpx: above the cover cap of 40 Mpx
        "html": b"<html><script>alert(1)</script></html>",
        "svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="9" height="9"/>',
        "empty": b"",
    }[body]
    report = bench.enrich()
    assert report.outcome == "enriched"  # the text still goes
    assert report.cover_rejected is not None and report.cover_rejected.startswith("image.")
    assert bench.field("cover") is None and bench.covers() == []
    assert bench.field("title") is not None


@pytest.mark.req("EF-BIB-013", "ENF-SEC-003")
def test_the_cover_never_bypasses_the_worker_whatever_the_decoder_says(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the decode step fails for any reason the bytes are not kept as a fallback."""
    from shelfhold.infrastructure.imaging import ImageRejected

    def refuse(*_a: object, **_k: object) -> None:
        raise ImageRejected("image.worker_failed", "worker", "worker")

    monkeypatch.setattr(enrich_module, "make_thumbnail", refuse)
    report = bench.enrich()
    assert report.cover_rejected == "image.worker_failed" and bench.covers() == []
    assert bench.field("cover") is None


@pytest.mark.req("EF-BIB-013", "ENF-SEC-003")
def test_a_provider_cannot_hand_over_a_cover_as_a_field(bench: Bench) -> None:
    """`cover` is the host's own field: a provider that proposes it (even declared) is ignored."""

    class Sneaky(OpenLibraryProvider):
        def __init__(self) -> None:
            super().__init__()
            self.manifest = self.manifest.model_copy(
                update={"touched_fields": self.manifest.touched_fields | {"cover", "age_rating"}}
            )

        async def identify(self, f: Any, target: EntityRef, hints: MatchHints) -> IdentifyResult:
            out = await super().identify(f, target, hints)
            prov = out.proposals[0].provenance
            extra = [
                FieldProposal(
                    entity_type="edition", field=name, value="1", confidence=1.0, provenance=prov
                )
                for name in ("cover", "age_rating", "rating")
            ]
            return IdentifyResult(proposals=[*out.proposals, *extra], relations=out.relations)

    bench.fake.covers.pop(ISBN)  # no cover on the server: only the sneaky one could set one
    bench.enrich(Sneaky())
    assert bench.field("cover") is None and bench.field("age_rating") is None
    assert bench.field("rating") is None and bench.field("title") is not None


# -- never over a locked or hand-typed value ---------------------------------------------------


def key(bench: Bench, field: str) -> FieldKey:
    return FieldKey("edition", bench.edition, field, "und")


@pytest.mark.req("EF-BIB-008", "EF-BIB-013", "ENF-LEG-005")
def test_a_locked_field_is_never_overwritten_by_openlibrary(bench: Bench) -> None:
    with bench.engine.begin() as connection:
        lock_field(connection, key(bench, "title"), "Mon titre", now=NOW)
        lock_field(connection, key(bench, "cover"), "covers/mine.webp", now=NOW)
    report = bench.enrich()
    title, cover = bench.field("title"), bench.field("cover")
    assert title is not None and title["value"] == '"Mon titre"'
    assert (title["provider"], title["provenance"], title["locked"]) == ("admin", "manual", 1)
    assert cover is not None and cover["value"] == '"covers/mine.webp"' and bench.covers() == []
    assert key(bench, "title") in report.kept_locked
    assert not [r for r in bench.fake.requests_to(COVER_HOST, "/b/")]  # not even asked for
    assert bench.field("publisher") is not None  # the free fields are filled


@pytest.mark.req("EF-BIB-008", "EF-BIB-013", "ENF-LEG-005")
def test_a_hand_typed_value_is_never_overwritten_even_after_the_lock_is_lifted(
    bench: Bench,
) -> None:
    """The provenance `manual` is what protects it, lock or not: OpenLibrary fills the empty."""
    from shelfhold.catalog.public import unlock_field

    with bench.engine.begin() as connection:
        lock_field(connection, key(bench, "publisher"), "Mon éditeur", now=NOW)
        unlock_field(connection, key(bench, "publisher"))
    report = bench.enrich()
    publisher = bench.field("publisher")
    assert publisher is not None and publisher["value"] == '"Mon éditeur"'
    assert (publisher["provider"], publisher["provenance"], publisher["locked"]) == (
        "admin",
        "manual",
        0,
    )
    assert key(bench, "publisher") in report.kept_manual
    assert bench.field("title") is not None  # nothing hand-typed: the provider fills it


@pytest.mark.req("EF-BIB-008", "EF-BIB-013")
def test_a_value_from_another_provider_is_replaced_and_the_same_one_twice_writes_nothing(
    bench: Bench,
) -> None:
    first = bench.enrich()
    again = bench.enrich()
    assert first.written and again.written == ()  # same value, same provider
    bench.fake.books[ISBN] = record(title="A newer title")
    bench.runtime.caches.clear()
    third = bench.enrich()
    assert key(bench, "title") in third.written
    title = bench.field("title")
    assert title is not None and title["value"] == '"A newer title"'


# -- an age is a suggestion, never applied -----------------------------------------------------


@pytest.mark.req("EF-BIB-013", "EF-BIB-055", "ENF-LEG-005")
def test_an_age_from_openlibrary_is_a_suggestion_never_applied(bench: Bench) -> None:
    with bench.engine.begin() as connection:
        connection.execute(sa.update(catalog.work).where(catalog.work.c.id == bench.work)
                           .values(age_rating=10, rated_at=NOW))  # fmt: skip
    bench.fake.books[ISBN] = record(subjects=[{"name": "Erotica"}])
    report = bench.enrich()
    (suggestion,) = bench.rows("SELECT * FROM age_suggestion")
    assert (suggestion["entity_type"], suggestion["entity_id"]) == ("edition", bench.edition)
    assert (suggestion["value"], suggestion["x"], suggestion["state"]) == (18, 0, "open")
    assert suggestion["provenance"] == "provider"
    (edition,) = bench.rows("SELECT age_rating, age_x, rated_at FROM edition")
    assert (edition["age_rating"], edition["age_x"], edition["rated_at"]) == (None, 0, None)
    (work,) = bench.rows("SELECT age_rating, age_x FROM work")
    assert (work["age_rating"], work["age_x"]) == (10, 0)  # the Work keeps the admin's rating
    assert [a.min_age for a in report.age_suggested] == [18]
    assert isinstance(report.withdrawals, tuple)  # the plan is told, nothing is withdrawn
    assert not bench.rows("SELECT * FROM audit")  # no rating happened: no rating audit


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_no_age_is_imported_from_the_record_whatever_it_claims(bench: Bench) -> None:
    bench.fake.books[ISBN] = record(age="0", age_rating="all", maturity_rating="G",
                                    subjects=[{"name": "Juvenile fiction"}])  # fmt: skip
    bench.enrich()
    assert bench.rows("SELECT * FROM age_suggestion") == []
    assert [r["field"] for r in bench.rows("SELECT field FROM metadata_field")
            if "age" in r["field"] or "rating" in r["field"]] == []  # fmt: skip
    (edition,) = bench.rows("SELECT age_rating, age_x, rated_at FROM edition")
    assert edition["age_rating"] is None and edition["rated_at"] is None


@pytest.mark.req("EF-BIB-013", "EF-BIB-055")
def test_a_provider_cannot_apply_an_age_through_a_relation(bench: Bench) -> None:
    class Pushy(OpenLibraryProvider):
        async def identify(self, f: Any, target: EntityRef, hints: MatchHints) -> IdentifyResult:
            out = await super().identify(f, target, hints)
            prov = out.proposals[0].provenance
            relation = RelationProposal(kind="age_suggestion", subject=target,
                                        data={"min_age": 0, "x": False, "apply": True},
                                        confidence=1.0, provenance=prov)  # fmt: skip
            return IdentifyResult(proposals=out.proposals, relations=[relation])

    bench.enrich(Pushy())
    (edition,) = bench.rows("SELECT age_rating, rated_at FROM edition")
    assert edition["age_rating"] is None and edition["rated_at"] is None
    assert [r["value"] for r in bench.rows("SELECT value FROM age_suggestion")] == [0]


# -- a provider that is down, refused or throttled ---------------------------------------------


@pytest.mark.req("EF-BIB-013", "ENF-OPS-011")
def test_metadata_provider_down_notifies_only(bench: Bench) -> None:
    bench.fake.status = 503
    report = bench.enrich()
    assert report.outcome == "provider_down" and report.reason == "http_503"
    assert bench.events() == ["metadata.provider_down"]
    (event,) = bench.rows("SELECT payload, visibility FROM event")
    assert "http_503" in str(event["payload"]) and "openlibrary" not in str(event["payload"])
    assert len(bench.rows("SELECT * FROM notification_pending")) == 1  # the notification
    assert bench.rows("SELECT * FROM metadata_field") == [] and bench.covers() == []
    assert bench.rows("SELECT * FROM job_view") == []  # nothing queued, paused or retried
    again = bench.enrich()  # the next try: still down, the admin is not told twice an hour
    assert again.outcome == "provider_down" and bench.events() == ["metadata.provider_down"]
    bench.clock.advance(seconds=DOWN_NOTICE_INTERVAL.total_seconds() + 1)
    bench.enrich()
    assert bench.events() == ["metadata.provider_down"] * 2
    bench.fake.status = 200  # it is back: the work goes on with no one to un-pause
    assert bench.enrich().outcome == "enriched"


@pytest.mark.req("EF-BIB-013", "ENF-OPS-011")
def test_an_unreachable_provider_is_down_too_and_a_missing_book_is_not(bench: Bench) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("synthetic")

    down = httpx2.MockTransport(refuse)
    fetcher = open_provider(build_manifest(), bench.fake.resolver, contact=None,
                            runtime=bench.runtime, transport=down)  # fmt: skip
    report = bench.enrich(fetcher=fetcher)
    assert report.outcome == "provider_down" and report.reason == "unreachable"
    bench.runtime.caches.clear()
    bench.runtime.robots.clear()  # robots.txt could not be read: asked again at the next try
    bench.fake.books.clear()
    missing = bench.enrich()
    assert missing.outcome == "not_found" and bench.events() == ["metadata.provider_down"]


@pytest.mark.req("EF-BIB-013", "ENF-LEG-003", "ENF-LEG-007")
def test_robots_respected_by_the_enrichment(bench: Bench) -> None:
    bench.fake.robots[API_HOST] = "User-agent: *\nDisallow: /api/\n"
    report = bench.enrich()
    assert report.outcome == "robots_disallowed"
    assert bench.fake.api_calls() == [] and bench.events() == []  # told, not an outage
    assert bench.rows("SELECT * FROM metadata_field") == []


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_the_enrichment_stops_when_the_bucket_is_empty_and_writes_nothing(bench: Bench) -> None:
    manifest = build_manifest().model_copy(update={"quota": build_manifest().quota.model_copy(
        update={"daily_cap": 2})})  # fmt: skip
    fetcher = open_provider(manifest, bench.fake.resolver, contact=None, runtime=bench.runtime,
                            transport=bench.fake.transport)  # fmt: skip
    first = bench.enrich(fetcher=fetcher)  # robots.txt + the call: the day's two tokens
    assert first.outcome == "enriched"
    bench.runtime.caches.clear()
    with bench.engine.begin() as connection:
        connection.execute(sa.delete(catalog.metadata_field))
    limited = bench.enrich(fetcher=fetcher)
    assert limited.outcome == "quota" and bench.events() == []
    assert bench.rows("SELECT * FROM metadata_field") == []


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_a_provider_without_an_identifying_agent_is_not_called(bench: Bench) -> None:
    class Spy(OpenLibraryProvider):
        called = False

        async def identify(self, f: Any, target: EntityRef, hints: MatchHints) -> IdentifyResult:
            Spy.called = True
            return IdentifyResult()

    anonymous = FakeFetcher({("GET", "https://openlibrary.org/x"): Response(status=200)})
    with pytest.raises(UserAgentNotIdentifiable):
        bench.enrich(Spy(), anonymous)
    assert Spy.called is False and bench.fake.seen == []


# -- the target --------------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-013")
def test_a_target_is_an_isbn_or_an_edition_never_a_guess(bench: Bench) -> None:
    service = bench.service
    with bench.engine.connect() as connection:
        assert service.resolve(connection, ISBN) == bench.edition
        assert service.resolve(connection, "978-0-306-40615-7") == bench.edition
        assert service.resolve(connection, bench.edition) == bench.edition
        for bad in (ISBN_OTHER, "9780306406158", "not-a-thing", "", "01NOSUCHEDITION000000000000"):
            with pytest.raises(UnknownTargetError):
                service.resolve(connection, bad)


@pytest.mark.req("EF-BIB-013")
def test_the_isbn_of_a_volume_resolves_to_its_edition(bench: Bench) -> None:
    with bench.engine.begin() as connection:
        connection.execute(sa.insert(catalog.volume).values(
            id=new_ulid(), edition_id=bench.edition, number="1", isbn=ISBN_OTHER))  # fmt: skip
    with bench.engine.connect() as connection:
        assert bench.service.resolve(connection, ISBN_OTHER) == bench.edition


@pytest.mark.req("EF-BIB-013", "ENF-LEG-007")
def test_the_second_enrichment_comes_from_the_cache(bench: Bench) -> None:
    bench.fake.cache_control = "max-age=3600"
    bench.enrich()
    calls = len(bench.fake.api_calls())
    bench.enrich()
    assert len(bench.fake.api_calls()) == calls == 1
