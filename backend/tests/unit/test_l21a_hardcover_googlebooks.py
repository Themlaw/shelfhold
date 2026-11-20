"""M4 L21a (EF-BIB-013, ENF-LEG-005): Hardcover and Google Books, alone, over fabricated servers.

Each has a key that is the instance's own: the Hardcover token and the Google Books key travel in
a header, never in an address, and are in nothing the provider returns, raises, prints or logs.
What a person types stays a literal; what the server says is believed for its shape only. Nothing
leaves the machine; no key is a literal here (they are drawn at run time).
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest
import structlog
from shelfhold_sdk.metadata import (
    EntityRef,
    IdentifyResult,
    MatchHints,
    ProviderCapability,
    ProviderUnavailable,
)

from shelfhold.plugin_host.adapters.native.googlebooks import PACIFIC, GoogleBooksProvider
from shelfhold.plugin_host.adapters.native.googlebooks import build_manifest as gb_manifest
from shelfhold.plugin_host.adapters.native.hardcover import HardcoverProvider
from shelfhold.plugin_host.adapters.native.hardcover import build_manifest as hc_manifest
from tests.fakes.cassette_server import CassetteServer
from tests.fakes.googlebooks import server as gb
from tests.fakes.hardcover import server as hc
from tests.l21b_bench import EDITION, FakeTime, asked, run, worker

pytestmark = pytest.mark.req("EF-BIB-013", "ENF-LEG-005")


@pytest.fixture
def key() -> str:
    return secrets.token_hex(16)  # made up for the test; never a literal


def hardcover(
    server: CassetteServer,
    key: str | None,
    *,
    title: str | None = hc.TITLE,
    ids: dict[str, str] | None = None,
) -> IdentifyResult:
    target = EntityRef(entity_type="edition", entity_id=EDITION, ids=ids or {})
    hints = MatchHints(title=title) if title else MatchHints()
    fetcher = worker(hc_manifest(), server, FakeTime())
    return run(HardcoverProvider(token=key).identify(fetcher, target, hints))  # type: ignore[arg-type]


def googlebooks(
    server: CassetteServer,
    key: str | None,
    *,
    title: str | None = gb.TITLE,
    ids: dict[str, str] | None = None,
    budget: int = 1000,
) -> IdentifyResult:
    target = EntityRef(entity_type="edition", entity_id=EDITION, ids=ids or {})
    hints = MatchHints(title=title) if title else MatchHints()
    fetcher = worker(gb_manifest(budget), server, FakeTime())
    provider = GoogleBooksProvider(api_key=key, daily_budget=budget)
    return run(provider.identify(fetcher, target, hints))  # type: ignore[arg-type]


def fields(result: IdentifyResult) -> dict[str, str]:
    return {p.field: p.value for p in result.proposals}


# -- Hardcover --------------------------------------------------------------------------------


def test_hardcover_manifest_60_a_minute_5000_a_day_and_one_domain() -> None:
    manifest = hc_manifest()
    assert manifest.quota.requests_per_second == 1.0  # 60 a minute
    assert manifest.quota.burst == 1
    assert manifest.quota.daily_cap == 5000
    assert [r.pattern for r in manifest.permissions.domains] == ["api.hardcover.app"]
    assert manifest.capabilities == ProviderCapability.IDENTIFY  # no cover, no account, no list
    assert manifest.auth is None
    assert manifest.terms_url and manifest.homepage == "https://hardcover.app"
    assert "Hardcover" in (manifest.attribution or "")


def test_hardcover_asks_by_title_with_the_token_in_a_header_only(key: str) -> None:
    server = hc.fake_hardcover()
    result = hardcover(server, key)
    [call] = server.calls()
    assert (call.method, call.host, call.path) == ("POST", hc.HOST, "/v1/graphql")
    assert call.headers["authorization"] == f"Bearer {key}"
    assert key not in call.path and key not in call.query and key not in call.body
    body = json.loads(call.body)
    assert body["variables"]["q"] == hc.TITLE  # what was typed is data, not glued into the query
    assert hc.TITLE not in body["query"]
    assert fields(result) == {
        "title": hc.TITLE,
        "description": "An invented book for tests.",
        "publisher": "Invented Press",
        "published_on": "1999",
    }
    proposal = result.proposals[0]
    assert proposal.provenance.source_url == hc.BOOK_URL
    assert "Hardcover" in (proposal.provenance.attribution or "")
    assert proposal.provenance.fetched_at is not None


def test_hardcover_asks_by_isbn_when_the_edition_has_one(key: str) -> None:
    server = hc.fake_hardcover()
    result = hardcover(server, key, title=None, ids={"isbn13": hc.ISBN})
    assert json.loads(server.calls()[0].body)["variables"]["q"] == hc.ISBN
    assert fields(result)["title"] == hc.TITLE


def test_hardcover_without_a_token_asks_nothing(key: str) -> None:
    server = hc.fake_hardcover()
    for empty in (None, "", "   "):
        with pytest.raises(ProviderUnavailable) as raised:
            hardcover(server, empty)
        assert raised.value.reason == "unauthorized"
    assert server.calls() == []


def test_hardcover_nothing_known_is_no_proposal_and_no_link(key: str) -> None:
    result = hardcover(hc.fake_hardcover(), key, title=hc.ABSENT)
    assert result.proposals == [] and result.relations == []


def test_hardcover_believes_the_shape_only(key: str) -> None:
    result = hardcover(hc.fake_hardcover(), key, title=hc.HOSTILE)
    found = fields(result)
    assert "title" not in found and "published_on" not in found  # not text, not a year
    assert found.get("description") == "Spaced out text"
    for proposal in result.proposals:
        assert proposal.provenance.source_url == "https://hardcover.app"  # a bad slug: home page


@pytest.mark.parametrize(
    ("cassette", "reason"),
    [
        ("unauthorized", "unauthorized"),
        ("over_limit", "rate_limited"),
        ("not_json", "schema_mismatch"),
    ],
)
def test_hardcover_names_its_errors_and_never_the_token(
    key: str, cassette: str, reason: str
) -> None:
    with structlog.testing.capture_logs() as events, pytest.raises(ProviderUnavailable) as raised:
        hardcover(hc.fake_hardcover(cassette), key)
    assert raised.value.reason == reason
    assert key not in repr(raised.value) and key not in repr(events)


def test_hardcover_status_5xx_and_other_statuses(key: str) -> None:
    server = hc.fake_hardcover()
    server.status = 503
    with pytest.raises(ProviderUnavailable) as raised:
        hardcover(server, key)
    assert raised.value.reason == "http_503"
    server.status = 404
    with pytest.raises(ProviderUnavailable) as other:
        hardcover(server, key)
    assert other.value.reason == "schema_mismatch"


def test_hardcover_repr_gives_the_token_away_nowhere(key: str) -> None:
    provider = HardcoverProvider(token=key)
    assert key not in repr(provider) and key not in str(vars(provider))
    assert "set" in repr(provider) and "unset" in repr(HardcoverProvider())


# -- Google Books -----------------------------------------------------------------------------


def test_googlebooks_manifest_budget_is_a_pacific_day_and_adjustable() -> None:
    manifest = gb_manifest()
    assert manifest.quota.daily_cap == 1000  # the cautious default of the spec
    assert manifest.quota.day_timezone == PACIFIC
    assert [r.pattern for r in manifest.permissions.domains] == ["www.googleapis.com"]
    assert manifest.capabilities == ProviderCapability.IDENTIFY
    assert gb_manifest(250).quota.daily_cap == 250
    for bad in (0, -1):
        with pytest.raises(ValueError):
            gb_manifest(bad)
    assert manifest.terms_url == "https://developers.google.com/books/terms"
    assert "Google Books" in (manifest.attribution or "")


def test_googlebooks_by_isbn_with_the_key_in_a_header_only(key: str) -> None:
    server = gb.fake_googlebooks()
    result = googlebooks(server, key, title=None, ids={"isbn13": gb.ISBN})
    [call] = server.calls()
    assert (call.method, call.host, call.path) == ("GET", gb.HOST, "/books/v1/volumes")
    assert asked(server)["q"] == f"isbn:{gb.ISBN}"
    assert call.headers["x-goog-api-key"] == key
    assert key not in call.query and key not in call.path  # never in an address
    assert fields(result) == {
        "title": gb.TITLE,
        "description": "An invented book for tests.",
        "publisher": "Invented Press",
        "published_on": "1999-05-01",
    }
    assert result.proposals[0].provenance.source_url == gb.VOLUME_URL


def test_googlebooks_by_title_keeps_what_was_typed_as_one_literal_parameter(key: str) -> None:
    server = gb.fake_googlebooks()
    googlebooks(server, key)
    assert asked(server)["q"] == f"intitle:{gb.TITLE}"
    hostile = "x&key=other&maxResults=40"
    with pytest.raises(AssertionError, match="no recorded answer"):  # no cassette: only the wire
        googlebooks(server, key, title=hostile)
    last = server.calls()[-1]
    assert last.query.count("key=") == 0 and last.query.count("maxResults=") == 1
    assert asked(server)["q"] == f"intitle:{hostile}"


def test_googlebooks_without_a_key_asks_nothing() -> None:
    server = gb.fake_googlebooks()
    for empty in (None, "", "  "):
        with pytest.raises(ProviderUnavailable) as raised:
            googlebooks(server, empty)
        assert raised.value.reason == "unauthorized"
    assert server.calls() == []


def test_googlebooks_nothing_known_and_hostile_answers(key: str) -> None:
    empty = googlebooks(gb.fake_googlebooks(), key, title=gb.ABSENT)
    assert empty.proposals == [] and empty.relations == []
    found = fields(googlebooks(gb.fake_googlebooks(), key, title=gb.HOSTILE))
    assert "title" not in found and "published_on" not in found and "publisher" not in found
    assert found.get("description") == "Spaced out text"


def test_googlebooks_a_429_per_day_is_told_apart_from_one_per_minute(key: str) -> None:
    with pytest.raises(ProviderUnavailable) as day:
        googlebooks(gb.fake_googlebooks("day_quota"), key)
    assert day.value.reason == "daily_quota"
    with pytest.raises(ProviderUnavailable) as minute:
        googlebooks(gb.fake_googlebooks("minute_quota"), key)
    assert minute.value.reason == "rate_limited"


@pytest.mark.parametrize(
    ("cassette", "reason"), [("bad_key", "unauthorized"), ("not_json", "schema_mismatch")]
)
def test_googlebooks_names_its_errors_and_never_the_key(
    key: str, cassette: str, reason: str
) -> None:
    with structlog.testing.capture_logs() as events, pytest.raises(ProviderUnavailable) as raised:
        googlebooks(gb.fake_googlebooks(cassette), key)
    assert raised.value.reason == reason
    assert key not in repr(raised.value) and key not in repr(events)


def test_googlebooks_repr_gives_the_key_away_nowhere(key: str) -> None:
    provider = GoogleBooksProvider(api_key=key)
    shown: Any = (repr(provider), str(vars(provider)))
    assert all(key not in text for text in shown)
