"""M4 L21b (EF-BIB-013, ENF-LEG-005): the BnF provider over a fabricated SRU and SPARQL.

The Fetcher is the one the host opens for a provider (guard, 1 request a second, cache, robots);
the server is a cassette of invented records in the shape of the real answers. Nothing leaves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from shelfhold.infrastructure.fetcher.errors import DomainNotAllowed, FetchTimeout, FetchUnreachable
from shelfhold.plugin_host.adapters.native.bnf import BnFProvider, build_manifest
from shelfhold.plugin_host.bnf_sru import read_sru
from shelfhold.plugin_host.provider_fetcher import ProviderRuntime, open_provider
from shelfhold.sdk.metadata import (
    EntityRef,
    IdentifyResult,
    MatchHints,
    ProviderCapability,
    ProviderUnavailable,
)
from tests.fakes.bnf.server import ABSENT, ISBN, SPARQL_ISBN, TITLE, fake_bnf
from tests.fakes.cassette_server import CassetteServer
from tests.fakes_fetcher import FakeResolver
from tests.l21b_bench import EDITION, FakeTime, asked, busiest_second, run, worker

ARK = "https://catalogue.bnf.fr/ark:/12148/cb00000001a"


def provider() -> BnFProvider:
    return BnFProvider(reader=read_sru)


def identify(
    server: CassetteServer,
    time: FakeTime,
    *,
    title: str | None = TITLE,
    ids: dict[str, str] | None = None,
) -> IdentifyResult:
    target = EntityRef(entity_type="edition", entity_id=EDITION, ids=ids or {})
    return run(
        provider().identify(worker(build_manifest(), server, time), target, MatchHints(title=title))
    )  # type: ignore[arg-type]


def fields(result: IdentifyResult) -> dict[str, object]:
    return {p.field: p.value for p in result.proposals}


# -- what comes back ---------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-013")
def test_a_title_gives_the_fields_of_the_first_notice_that_may_be_proposed() -> None:
    time = FakeTime()
    result = identify(fake_bnf(clock=time.now), time)
    assert fields(result) == {
        "title": "The Synthetic Chronicle",  # the title proper, not the statement of responsibility
        "contributors": ["Example, Ada", "Sample, Bo"],  # no dates, no role
        "publisher": "Example Press",
        "published_on": "2001",
        "subjects": ["Fiction", "Fantasy"],  # the Dewey subjects are gone, the others stay
    }
    assert {p.entity_type for p in result.proposals} == {"edition"}
    assert all(0 < p.confidence < 1 for p in result.proposals)


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_every_proposal_carries_the_source_the_date_and_the_licence() -> None:
    before = datetime.now(UTC) - timedelta(seconds=1)
    time = FakeTime()
    result = identify(fake_bnf(clock=time.now), time)
    after = datetime.now(UTC) + timedelta(seconds=1)
    assert result.proposals and result.relations
    for item in (*result.proposals, *result.relations):
        provenance = item.provenance
        assert provenance.provider == "bnf"
        assert provenance.source_url == ARK  # the notice itself, on the catalogue, over https
        assert provenance.fetched_at.tzinfo is not None
        assert before <= provenance.fetched_at <= after  # the instant of the retrieval
        assert provenance.licence == "Licence Ouverte"
        assert "BnF" in (provenance.attribution or "")
    (link,) = result.relations
    assert (link.kind, link.data) == ("official_link", {"url": ARK})


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_dewey_notices_and_dewey_subjects_are_never_proposed() -> None:
    time = FakeTime()
    result = identify(fake_bnf(clock=time.now), time)
    answer = result.model_dump(mode="json")
    for item in (*answer["proposals"], *answer["relations"]):
        # the instant of the retrieval is the host's wall clock (the test above checks it): its
        # digits are not the provider's content, and "843" may show up in them by chance
        item["provenance"]["fetched_at"] = "the retrieval instant"
    flat = repr(answer).lower()
    assert "classification notice" not in flat and "classification table" not in flat
    assert "843" not in flat and "dewey" not in flat
    assert ARK in flat and "cb00000002b" not in flat  # the notice that was skipped is not linked


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_no_cover_and_no_gallica_address_ever() -> None:
    time = FakeTime()
    server = fake_bnf(clock=time.now)
    result = identify(server, time)
    assert "gallica" not in repr(result.model_dump(mode="json")).lower()
    manifest = build_manifest()
    assert ProviderCapability.COVER not in manifest.capabilities
    assert all("gallica" not in d.pattern for d in manifest.permissions.domains)
    fetcher = worker(manifest, server, time)
    target = EntityRef(entity_type="edition", entity_id=EDITION)
    assert run(provider().get_cover(fetcher, target)) is None  # type: ignore[arg-type]
    with pytest.raises(DomainNotAllowed):  # the host would not let it through either
        run(fetcher.get("https://gallica.bnf.fr/ark:/12148/bpt6k0000001"))
    assert [c.host for c in server.calls()] == ["catalogue.bnf.fr"]


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_the_manifest_declares_the_terms_the_attribution_and_the_rate() -> None:
    manifest = build_manifest()
    assert manifest.terms_url == "https://data.bnf.fr/fr/licence"
    assert "BnF" in (manifest.attribution or "") and "Licence Ouverte" in manifest.attribution
    assert {d.pattern for d in manifest.permissions.domains} == {"catalogue.bnf.fr", "data.bnf.fr"}
    assert manifest.auth is None and manifest.capabilities == ProviderCapability.IDENTIFY
    assert (manifest.quota.requests_per_second, manifest.quota.burst) == (1.0, 1)
    assert manifest.respects_robots


# -- the doors ---------------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-013")
def test_a_title_is_asked_to_the_sru_with_a_quoted_cql_term() -> None:
    time = FakeTime()
    server = fake_bnf(clock=time.now)
    identify(server, time)
    (call,) = server.calls()
    assert (call.method, call.host, call.path) == ("GET", "catalogue.bnf.fr", "/api/SRU")
    sent = asked(server)
    assert sent["operation"] == "searchRetrieve" and sent["version"] == "1.2"
    assert sent["query"] == f'bib.title all "{TITLE}"'
    assert int(sent["maximumRecords"]) <= 10  # a lookup, never a harvest
    assert "authorization" not in {name.lower() for name in call.headers}


@pytest.mark.req("EF-BIB-013")
def test_an_isbn_alone_is_asked_to_the_sru_by_isbn() -> None:
    time = FakeTime()
    server = fake_bnf(clock=time.now)
    result = identify(server, time, title=None, ids={"isbn13": ISBN})
    assert asked(server)["query"] == f'bib.isbn all "{ISBN}"'
    assert fields(result)["title"] == "The Synthetic Chronicle"
    assert len(server.calls()) == 1  # found at the first door: the second is not asked


@pytest.mark.req("EF-BIB-013")
def test_the_sparql_door_answers_when_the_sru_does_not_know_the_isbn() -> None:
    time = FakeTime()
    server = fake_bnf(clock=time.now)
    result = identify(server, time, title=None, ids={"isbn13": SPARQL_ISBN})
    assert [c.host for c in server.calls()] == ["catalogue.bnf.fr", "data.bnf.fr"]
    assert server.calls()[1].path == "/sparql"
    sent = asked(server)
    assert SPARQL_ISBN in sent["query"] and sent["query"].lstrip().upper().startswith("SELECT")
    assert fields(result) == {
        "title": "Sparql Synthetic Title",
        "contributors": ["Example, Ada"],
        "publisher": "Example Press",
        "published_on": "1999",
    }  # its "Dewey 800" subject is not proposed
    assert {p.provenance.source_url for p in result.proposals} == {
        "https://data.bnf.fr/ark:/12148/cb00000004d"
    }
    assert all(p.provenance.licence == "Licence Ouverte" for p in result.proposals)


@pytest.mark.req("EF-BIB-013")
@pytest.mark.parametrize("isbn", ['978"x', "12345", "", "9780306406157 OR 1", "97803064061570"])
def test_a_value_that_is_not_an_isbn_is_never_sent(isbn: str) -> None:
    time = FakeTime()
    server = fake_bnf(clock=time.now)
    result = identify(server, time, title=None, ids={"isbn13": isbn})
    assert result == IdentifyResult() and server.calls() == []


@pytest.mark.req("EF-BIB-013")
@pytest.mark.parametrize("title", [None, "", "   \n\t", "\x00\x1f"])
def test_nothing_to_ask_by_is_no_request(title: str | None) -> None:
    time = FakeTime()
    server = fake_bnf(clock=time.now)
    assert identify(server, time, title=title) == IdentifyResult()
    assert server.calls() == []


# -- nothing, or not what was expected ---------------------------------------------------------


@pytest.mark.req("EF-BIB-013")
def test_a_title_the_bnf_does_not_know_is_an_empty_answer() -> None:
    time = FakeTime()
    assert identify(fake_bnf(clock=time.now), time, title=ABSENT) == IdentifyResult()


@pytest.mark.req("EF-BIB-013")
def test_notices_with_no_usable_title_are_skipped_and_a_missing_address_is_no_link() -> None:
    time = FakeTime()
    result = identify(fake_bnf("absent", clock=time.now), time)
    assert fields(result) == {"title": "Bare Title"}  # no title, a blank title: skipped
    assert result.relations == []  # an address that is not a notice's is not linked
    assert {p.provenance.source_url for p in result.proposals} == {"https://catalogue.bnf.fr/"}


@pytest.mark.req("EF-BIB-013")
@pytest.mark.parametrize("name", ["malformed", "entities"])
def test_an_answer_that_is_not_safe_xml_is_a_schema_mismatch(name: str) -> None:
    time = FakeTime()
    with pytest.raises(ProviderUnavailable) as raised:
        identify(fake_bnf(name, clock=time.now), time)  # a broken document; entities declared
    assert raised.value.reason == "schema_mismatch"


@pytest.mark.req("EF-BIB-013")
def test_a_sparql_answer_of_the_wrong_shape_is_a_schema_mismatch() -> None:
    time = FakeTime()
    server = fake_bnf("sparql_malformed", clock=time.now)
    with pytest.raises(ProviderUnavailable) as raised:
        identify(server, time, title=None, ids={"isbn13": ISBN})
    assert raised.value.reason == "schema_mismatch"


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_provider_that_is_down_or_throttling_is_unavailable(status: int) -> None:
    time = FakeTime()
    with pytest.raises(ProviderUnavailable) as raised:
        identify(fake_bnf(clock=time.now, status=status), time)
    assert raised.value.reason == "http_503"


@pytest.mark.req("EF-BIB-013")
@pytest.mark.parametrize("status", [400, 403, 404])
def test_a_refusal_of_the_question_is_no_answer_not_an_outage(status: int) -> None:
    time = FakeTime()
    assert identify(fake_bnf(clock=time.now, status=status), time) == IdentifyResult()


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
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
        build_manifest(), FakeResolver({"catalogue.bnf.fr": ["93.184.216.34"]}), contact=None,
        runtime=runtime, transport=httpx2.MockTransport(broken),
    )  # fmt: skip
    target = EntityRef(entity_type="edition", entity_id=EDITION)
    with pytest.raises(expected):
        run(provider().identify(fetcher, target, MatchHints(title=TITLE)))  # type: ignore[arg-type]


# -- politeness --------------------------------------------------------------------------------


@pytest.mark.req("ENF-LEG-005")
def test_one_request_a_second_whatever_is_asked() -> None:
    time = FakeTime()
    server = fake_bnf(clock=time.now)
    fetcher = worker(build_manifest(), server, time)
    target = EntityRef(entity_type="edition", entity_id=EDITION)
    for n in range(6):
        title = TITLE if n % 2 else ABSENT
        run(provider().identify(fetcher, target, MatchHints(title=f"{title} {n}")))  # type: ignore[arg-type]
    assert len(server.calls()) == 6
    assert busiest_second(server) <= 1
    assert server.calls()[-1].at - server.calls()[0].at >= 5  # six requests, five seconds


# -- addresses ---------------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-013")
def test_a_notice_address_is_read_and_written_only_when_it_is_one() -> None:
    p = provider()
    assert p.url_for({"bnf": "cb00000001a"}) == ARK
    assert p.url_for({"ark": "cb00000001a"}) == ARK
    assert p.url_for({"bnf": 'cb1"><script>'}) is None and p.url_for({}) is None
    assert p.ids_from_url(ARK) == {"bnf": "cb00000001a"}
    assert p.ids_from_url(ARK + "/date?x=1") == {"bnf": "cb00000001a"}
    assert p.ids_from_url("https://gallica.bnf.fr/ark:/12148/bpt6k0000001") is None
    assert p.ids_from_url("https://example.org/ark:/12148/cb00000001a") is None
    assert p.ids_from_url("https://catalogue.bnf.fr/ark:/12148/") is None
    assert p.ids_from_url("https://catalogue.bnf.fr/ark:/12148/cb1%22x") is None


@pytest.mark.req("EF-BIB-013")
def test_the_bnf_is_read_anonymously() -> None:
    time = FakeTime()
    fetcher = worker(build_manifest(), fake_bnf(clock=time.now), time)
    with pytest.raises(NotImplementedError):
        run(provider().login(fetcher, "oauth", {}))  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        run(provider().refresh(fetcher, None))  # type: ignore[arg-type]
