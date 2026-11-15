"""M4 L22 (EF-BIB-013, ENF-LEG-005): the ComicVine provider, alone, over a fabricated server.

Non-commercial, one key per instance, a link back, 200 requests an hour. What a person types stays
a literal; what the server says is believed for its shape only; the key is never in anything the
provider returns, raises or logs. Nothing leaves the machine.
"""

from __future__ import annotations

import json
import secrets
from urllib.parse import parse_qs

import pytest
import structlog

from shelfhold.plugin_host.adapters.native.comicvine import ComicVineProvider, build_manifest
from shelfhold.sdk.metadata import (
    EntityRef,
    IdentifyResult,
    MatchHints,
    ProviderCapability,
    ProviderUnavailable,
)
from tests.fakes.cassette_server import CassetteServer
from tests.fakes.comicvine.server import ABSENT, HOSTILE, TITLE, VOLUME_URL, fake_comicvine
from tests.l21b_bench import EDITION, FakeTime, run, worker


@pytest.fixture
def key() -> str:
    return secrets.token_hex(16)  # made up for the test; never a literal


def identify(
    server: CassetteServer,
    time: FakeTime,
    key: str | None,
    *,
    title: str | None = TITLE,
    ids: dict[str, str] | None = None,
) -> IdentifyResult:
    target = EntityRef(entity_type="work", entity_id=EDITION, ids=ids or {})
    hints = MatchHints(title=title) if title else MatchHints()
    fetcher = worker(build_manifest(), server, time)
    return run(ComicVineProvider(api_key=key).identify(fetcher, target, hints))  # type: ignore[arg-type]


def asked(server: CassetteServer, index: int = -1) -> dict[str, list[str]]:
    return parse_qs(server.calls()[index].query, keep_blank_values=True)


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_manifest_states_its_conditions() -> None:
    manifest = build_manifest()
    assert manifest.terms_url == "https://comicvine.gamespot.com/api/documentation"
    assert manifest.homepage == "https://comicvine.gamespot.com"
    assert "non-commercial" in (manifest.attribution or "").lower()
    assert "comicvine.gamespot.com" in (manifest.attribution or "")
    assert [d.pattern for d in manifest.permissions.domains] == ["comicvine.gamespot.com"]
    assert manifest.respects_robots is True
    assert manifest.capabilities == ProviderCapability.IDENTIFY  # no cover, no account
    assert manifest.quota.requests_per_second * 3600 == pytest.approx(200)  # per hour, not more
    assert manifest.quota.burst == 1
    props = manifest.settings_schema["properties"]["api_key"]  # one key per instance, a secret
    assert props["writeOnly"] is True and props["format"] == "password"
    assert manifest.settings_schema["required"] == ["api_key"]


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_identify_proposes_with_provenance_and_a_link_back(key: str) -> None:
    server, time = fake_comicvine(), FakeTime()
    result = identify(server, time, key)
    fields = {p.field: p.value for p in result.proposals}
    assert fields == {
        "title": TITLE, "description": "An invented series for tests.",
        "publisher": "Invented Press", "published_on": "1999",
    }  # fmt: skip
    for proposal in result.proposals:
        assert proposal.provenance.provider == "comicvine"
        assert proposal.provenance.source_url == VOLUME_URL
        assert "non-commercial" in (proposal.provenance.attribution or "").lower()
        assert proposal.confidence < 1
    (link,) = result.relations  # the link back: the volume's own page on the site
    assert link.kind == "official_link" and link.data == {"url": VOLUME_URL}
    assert [c.host for c in server.calls()] == ["comicvine.gamespot.com"]


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_what_a_person_types_stays_a_literal(key: str) -> None:
    typed = f"a&api_key=other&limit=999#x y=z{chr(10)}%26"
    server = fake_comicvine("not_json")
    with pytest.raises(ProviderUnavailable):
        identify(server, FakeTime(), key, title=typed)
    query = asked(server)
    assert query["api_key"] == [key] and query["limit"] == ["5"]  # nothing was overridden
    assert query["resources"] == ["volume"] and len(query["query"]) == 1
    assert query["query"][0].startswith("a&api_key=other&limit=999#x y=z")
    assert set(query) == {"api_key", "format", "limit", "query", "resources"}


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_asks_nothing_without_a_key_or_without_a_title() -> None:
    server, time = fake_comicvine(), FakeTime()
    for missing in (None, "", "   "):
        with pytest.raises(ProviderUnavailable) as raised:
            identify(server, time, missing)
        assert raised.value.reason == "unauthorized"
    assert identify(server, time, "k" * 32, title=None) == IdentifyResult()
    assert identify(server, time, "k" * 32, title="  ") == IdentifyResult()
    assert server.calls() == []  # not one request


@pytest.mark.req("EF-BIB-013")
def test_comicvine_not_found_gives_nothing_not_even_a_link(key: str) -> None:
    assert identify(fake_comicvine(), FakeTime(), key, title=ABSENT) == IdentifyResult()


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_comicvine_trusts_an_answer_for_its_shape_only(key: str) -> None:
    result = identify(fake_comicvine(), FakeTime(), key, title=HOSTILE)
    fields = {p.field: p.value for p in result.proposals}
    # the name is not text, the year is not a year, the publisher is not an object: dropped
    assert fields == {"description": "Spaced out text"}
    for proposal in result.proposals:  # an address of another host is never the source
        assert proposal.provenance.source_url == "https://comicvine.gamespot.com"
    assert all("elsewhere" not in json.dumps(r.data) for r in result.relations)


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
@pytest.mark.parametrize("cassette", ["malformed", "malformed_item", "not_json"])
def test_comicvine_malformed_data_is_a_schema_mismatch(cassette: str, key: str) -> None:
    with pytest.raises(ProviderUnavailable) as raised:
        identify(fake_comicvine(cassette), FakeTime(), key)
    assert raised.value.reason == "schema_mismatch"


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "unauthorized"), (403, "unauthorized"), (429, "rate_limited"), (500, "http_503"),
     (503, "http_503")],
)  # fmt: skip
def test_comicvine_http_errors_are_closed_reasons(status: int, reason: str, key: str) -> None:
    server = fake_comicvine(status=status)
    with pytest.raises(ProviderUnavailable) as raised:
        identify(server, FakeTime(), key)
    assert raised.value.reason == reason


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
@pytest.mark.parametrize(("cassette", "reason"), [("bad_key", "unauthorized"),
                                                   ("over_limit", "rate_limited")])  # fmt: skip
def test_comicvine_says_an_error_in_a_200_answer_too(cassette: str, reason: str, key: str) -> None:
    with pytest.raises(ProviderUnavailable) as raised:
        identify(fake_comicvine(cassette), FakeTime(), key)
    assert raised.value.reason == reason  # the text of its error is never repeated
    assert str(raised.value) == reason  # not one word of its own message


@pytest.mark.req("ENF-LEG-005")
def test_comicvine_spends_no_more_than_200_requests_an_hour(key: str) -> None:
    server, time = fake_comicvine(), FakeTime()
    fetcher = worker(build_manifest(), server, time)
    provider = ComicVineProvider(api_key=key)
    target = EntityRef(entity_type="work", entity_id=EDITION)
    for _ in range(260):  # a loop that does not wait: the host's bucket does it
        run(provider.identify(fetcher, target, MatchHints(title=TITLE)))  # type: ignore[arg-type]
    times = [c.at for c in server.calls()]
    assert len(times) == 260  # a request that carries a key is never served from a cache
    window = max(sum(1 for x in times if start <= x < start + 3600) for start in times)
    assert window <= 201  # the bucket (burst 1) never lets an hour hold more than its 200 + 1
    assert time.t - 10_000 >= 259 * 18 - 1  # and it really waited: one every 18 seconds


@pytest.mark.req("ENF-SEC-005", "ENF-LEG-005")
@pytest.mark.parametrize(
    "mode", ["search", "bad_key", "over_limit", "malformed", "not_json", "401", "429", "500"]
)  # fmt: skip
def test_comicvine_key_is_in_no_output_log_or_error(mode: str, key: str) -> None:
    status = int(mode) if mode.isdigit() else None
    server = fake_comicvine("search" if status else mode, status=status)
    with structlog.testing.capture_logs() as events:
        try:
            result: object = identify(server, FakeTime(), key)
        except ProviderUnavailable as error:
            result = error
            assert key not in repr(error) and key not in str(error)
    assert key not in repr(result) and key not in json.dumps(events, default=repr)
    (call,) = server.calls()
    assert [v for k, v in call.headers.items() if key in v] == []  # only the query carries it
    assert key not in call.path and parse_qs(call.query)["api_key"] == [key]


@pytest.mark.req("ENF-SEC-005")
def test_comicvine_provider_does_not_show_its_key(key: str) -> None:
    provider = ComicVineProvider(api_key=key)
    for shown in (repr(provider), str(provider), repr(vars(provider)), repr(provider.manifest)):
        assert key not in shown


@pytest.mark.req("EF-BIB-013")
def test_comicvine_addresses_are_ones_of_the_site_only() -> None:
    provider = ComicVineProvider()
    assert provider.url_for({"comicvine": "4050-777001"}) == (
        "https://comicvine.gamespot.com/volume/4050-777001/"
    )
    assert provider.ids_from_url(VOLUME_URL) == {"comicvine": "4050-777001"}
    for junk in ({"comicvine": "../x"}, {"comicvine": "javascript:1"}, {"comicvine": ""}, {}):
        assert provider.url_for(junk) is None
    for foreign in (
        "http://comicvine.gamespot.com/x/4050-1/",
        "https://comicvine.gamespot.com.evil.invalid/x/4050-1/",
        "https://comicvine.gamespot.com/x/",
        "javascript:alert(1)",
    ):
        assert provider.ids_from_url(foreign) is None
