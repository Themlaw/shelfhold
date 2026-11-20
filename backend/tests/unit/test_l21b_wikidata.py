"""M4 L21b (EF-BIB-003, EF-BIB-013): Wikidata gives correspondences, as suggestions, and no more.

Never a field, never an identity: the answer holds links and the identifiers of other catalogues
that the host shows and an admin may accept. The server is a cassette of invented items.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from shelfhold_sdk.metadata import (
    EntityRef,
    IdentifyResult,
    MatchHints,
    ProviderCapability,
    ProviderUnavailable,
)

from shelfhold.infrastructure.fetcher.errors import FetchTimeout, FetchUnreachable
from shelfhold.plugin_host.adapters.native.wikidata import WikidataProvider, build_manifest
from shelfhold.plugin_host.provider_fetcher import ProviderRuntime, open_provider
from tests.fakes.cassette_server import CassetteServer
from tests.fakes.wikidata.server import ABSENT, FOREIGN, HOSTILE, QID, TITLE, TWIN, fake_wikidata
from tests.fakes_fetcher import FakeResolver
from tests.l21b_bench import EDITION, FakeTime, asked, busiest_second, run, worker

ENTITY = "https://www.wikidata.org/entity/"


def identify(
    server: CassetteServer,
    time: FakeTime,
    *,
    title: str | None = TITLE,
    ids: dict[str, str] | None = None,
    entity_type: str = "work",
) -> IdentifyResult:
    target = EntityRef(entity_type=entity_type, entity_id=EDITION, ids=ids or {})  # type: ignore[arg-type]
    fetcher = worker(build_manifest(), server, time)
    return run(WikidataProvider().identify(fetcher, target, MatchHints(title=title)))  # type: ignore[arg-type]


@pytest.mark.req("EF-BIB-003", "EF-BIB-013")
def test_an_item_comes_back_as_one_low_confidence_link_with_its_correspondences() -> None:
    before = datetime.now(UTC) - timedelta(seconds=1)
    time = FakeTime()
    result = identify(fake_wikidata(clock=time.now), time)
    after = datetime.now(UTC) + timedelta(seconds=1)
    assert result.proposals == []  # not one field: a catalogue's own values are never replaced
    (link,) = result.relations
    assert link.kind == "official_link" and link.object is None
    assert link.data == {
        "url": f"{ENTITY}{QID}",
        "correspondences": {"viaf": "100000001", "bnf": "00000001a", "openlibrary": "OL1W"},
    }
    assert link.confidence <= 0.5  # a suggestion
    assert (link.subject.entity_type, link.subject.entity_id) == ("work", EDITION)
    provenance = link.provenance
    assert provenance.provider == "wikidata" and provenance.source_url == f"{ENTITY}{QID}"
    assert provenance.licence == "CC0-1.0" and "Wikidata" in (provenance.attribution or "")
    assert before <= provenance.fetched_at <= after


@pytest.mark.req("EF-BIB-003")
def test_the_qid_is_never_put_among_the_identifiers_of_the_target() -> None:
    time = FakeTime()
    server = fake_wikidata(clock=time.now)
    result = identify(server, time, ids={"isbn13": "9780306406157"}, entity_type="edition")
    (link,) = result.relations
    assert link.subject.ids == {"isbn13": "9780306406157"}  # unchanged: Wikidata is not a pivot
    assert QID not in repr(link.subject)
    assert QID not in repr(link.data["correspondences"])


@pytest.mark.req("EF-BIB-003", "EF-BIB-013")
def test_the_manifest_is_a_read_only_lookup_with_nothing_to_write() -> None:
    manifest = build_manifest()
    assert manifest.auth is None and manifest.capabilities == ProviderCapability.IDENTIFY
    assert manifest.touched_fields == set()  # the host keeps no field from it
    assert [d.pattern for d in manifest.permissions.domains] == ["query.wikidata.org"]
    assert "Wikidata" in (manifest.attribution or "") and manifest.terms_url
    assert (manifest.quota.requests_per_second, manifest.quota.burst) == (1.0, 1)


@pytest.mark.req("EF-BIB-003")
def test_the_title_goes_in_one_quoted_literal_of_a_read_query() -> None:
    time = FakeTime()
    server = fake_wikidata(clock=time.now)
    identify(server, time)
    (call,) = server.calls()
    assert (call.method, call.host, call.path) == ("GET", "query.wikidata.org", "/sparql")
    sent = asked(server)
    assert sent["format"] == "json"
    assert sent["query"].lstrip().upper().startswith("SELECT")
    assert f'"{TITLE}"@en' in sent["query"]
    assert "authorization" not in {name.lower() for name in call.headers}


@pytest.mark.req("EF-BIB-003")
def test_two_items_with_the_same_label_are_no_suggestion_at_all() -> None:
    time = FakeTime()
    assert identify(fake_wikidata(clock=time.now), time, title=TWIN) == IdentifyResult()


@pytest.mark.req("EF-BIB-003")
def test_an_unknown_title_is_an_empty_answer() -> None:
    time = FakeTime()
    assert identify(fake_wikidata(clock=time.now), time, title=ABSENT) == IdentifyResult()


@pytest.mark.req("EF-BIB-003")
def test_identifiers_that_are_not_what_they_claim_are_dropped_not_trusted() -> None:
    time = FakeTime()
    result = identify(fake_wikidata(clock=time.now), time, title=HOSTILE)
    (link,) = result.relations
    assert link.data == {"url": f"{ENTITY}Q900009"}  # all three values refused; the link stays


@pytest.mark.req("EF-BIB-003")
def test_an_item_that_is_not_an_entity_of_wikidata_is_ignored() -> None:
    time = FakeTime()
    assert identify(fake_wikidata(clock=time.now), time, title=FOREIGN) == IdentifyResult()


@pytest.mark.req("EF-BIB-003")
def test_a_known_qid_is_asked_by_its_number_only_when_it_is_one() -> None:
    time = FakeTime()
    server = fake_wikidata(clock=time.now)
    result = identify(server, time, title=None, ids={"wikidata": QID})
    assert f"wd:{QID}" in asked(server)["query"]
    (link,) = result.relations
    assert link.data["correspondences"] == {"viaf": "100000001"}
    for bad in ('Q1"} # ', "Q", "42", "Q1 Q2", "q900001; DROP", ""):
        quiet = fake_wikidata(clock=time.now)
        assert identify(quiet, time, title=None, ids={"wikidata": bad}) == IdentifyResult()
        assert quiet.calls() == []  # nothing of it reaches a query


@pytest.mark.req("EF-BIB-003")
@pytest.mark.parametrize("title", [None, "", " \n ", "\x00\x1f"])
def test_nothing_to_ask_by_is_no_request(title: str | None) -> None:
    time = FakeTime()
    server = fake_wikidata(clock=time.now)
    assert identify(server, time, title=title) == IdentifyResult()
    assert server.calls() == []


@pytest.mark.req("EF-BIB-003")
def test_an_answer_of_the_wrong_shape_is_a_schema_mismatch() -> None:
    time = FakeTime()
    with pytest.raises(ProviderUnavailable) as raised:
        identify(fake_wikidata("malformed", clock=time.now), time)
    assert raised.value.reason == "schema_mismatch"


@pytest.mark.req("EF-BIB-003", "ENF-LEG-005")
@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_provider_that_is_down_or_throttling_is_unavailable(status: int) -> None:
    time = FakeTime()
    with pytest.raises(ProviderUnavailable) as raised:
        identify(fake_wikidata(clock=time.now, status=status), time)
    assert raised.value.reason == "http_503"
    assert identify(fake_wikidata(clock=time.now, status=404), time) == IdentifyResult()


@pytest.mark.req("EF-BIB-003", "ENF-LEG-005")
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx2.ConnectError("refused"), FetchUnreachable),
        (httpx2.ReadTimeout("slow"), FetchTimeout),
    ],
)
def test_a_network_failure_reaches_the_host_as_a_fetch_error(
    failure: Exception, expected: type[Exception]
) -> None:
    time = FakeTime()

    def broken(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/robots.txt":
            return httpx2.Response(404)
        raise failure

    runtime = ProviderRuntime(clock=time.now, sleep=time.sleep, wall=time.now)
    fetcher = open_provider(
        build_manifest(), FakeResolver({"query.wikidata.org": ["93.184.216.34"]}), contact=None,
        runtime=runtime, transport=httpx2.MockTransport(broken),
    )  # fmt: skip
    target = EntityRef(entity_type="work", entity_id=EDITION)
    with pytest.raises(expected):
        run(WikidataProvider().identify(fetcher, target, MatchHints(title=TITLE)))  # type: ignore[arg-type]


@pytest.mark.req("ENF-LEG-005")
def test_one_request_a_second_whatever_is_asked() -> None:
    time = FakeTime()
    server = fake_wikidata(clock=time.now)
    runtime = ProviderRuntime(clock=time.now, sleep=time.sleep, wall=time.now)
    target = EntityRef(entity_type="work", entity_id=EDITION)
    fetcher = worker(build_manifest(), server, time, runtime)
    for title in (TITLE, ABSENT, TWIN, HOSTILE, FOREIGN):
        run(WikidataProvider().identify(fetcher, target, MatchHints(title=title)))  # type: ignore[arg-type]
    assert len(server.calls()) == 5
    assert busiest_second(server) <= 1
    assert server.calls()[-1].at - server.calls()[0].at >= 4  # five requests, four seconds


@pytest.mark.req("EF-BIB-003")
def test_an_entity_address_is_read_and_written_only_when_it_is_one() -> None:
    p = WikidataProvider()
    assert p.url_for({"wikidata": "q900001"}) == f"{ENTITY}{QID}"
    assert p.url_for({"qid": QID}) == f"{ENTITY}{QID}"
    assert p.url_for({"wikidata": 'Q1"'}) is None and p.url_for({}) is None
    assert p.ids_from_url(f"{ENTITY}{QID}") == {"wikidata": QID}
    assert p.ids_from_url(f"https://www.wikidata.org/wiki/{QID}?uselang=fr") == {"wikidata": QID}
    assert p.ids_from_url("https://www.wikidata.org/wiki/Special:Search") is None
    assert p.ids_from_url(f"https://evil.example/entity/{QID}") is None
