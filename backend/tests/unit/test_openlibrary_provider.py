"""L13 (EF-BIB-013, ENF-LEG-005): the OpenLibrary provider, a native metadata adapter.

It answers an ISBN with proposals (never a rating), declares what it touches, names its terms and
its attribution, and passes the conformance kit of the SDK on a scripted Fetcher.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Awaitable
from pathlib import Path

import pytest

from shelfhold.plugin_host.adapters.native import openlibrary as package
from shelfhold.plugin_host.adapters.native.openlibrary import (
    OpenLibraryProvider,
    build_manifest,
    quota_for,
)
from shelfhold.sdk.fetcher import Response
from shelfhold.sdk.metadata import EntityRef, MatchHints, ProviderCapability, ProviderUnavailable
from shelfhold.sdk.testing import FakeFetcher, run_metadata_conformance
from tests.fakes.openlibrary.server import API_HOST, COVER_HOST, ISBN, OL_ID, record

REPO = Path(__file__).resolve().parents[3]
BOOKS = f"https://{API_HOST}/api/books?bibkeys=ISBN:{ISBN}&format=json&jscmd=data"
TARGET = EntityRef(entity_type="edition", ids={"isbn13": ISBN})


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


def fetcher(body: object, status: int = 200) -> FakeFetcher:
    return FakeFetcher({("GET", BOOKS): Response(status=status, body=json.dumps(body).encode())})


@pytest.mark.req("EF-BIB-013")
def test_openlibrary_passes_the_metadata_conformance_kit() -> None:
    report = run(
        run_metadata_conformance(
            OpenLibraryProvider(),
            fetcher({f"ISBN:{ISBN}": record()}),
            targets=[TARGET],
            hints=MatchHints(title="Synthetic title"),
        )
    )
    report.assert_ok()
    assert {c.name for c in report.checks} >= {"proposals_typed", "isbn_targets_edition", "offline"}


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_the_manifest_declares_terms_attribution_quota_and_domains() -> None:
    m = build_manifest()
    assert m.key == "openlibrary" and m.kind.value == "metadata"
    assert str(m.terms_url).startswith("https://openlibrary.org/")
    assert m.attribution and "Open Library" in m.attribution
    assert m.respects_robots is True
    assert ProviderCapability.COVER in m.capabilities and ProviderCapability.IDENTIFY in m.capabilities
    assert (m.quota.requests_per_second, m.quota.daily_cap) == (1.0, m.quota.daily_cap)
    assert m.quota == quota_for(None) and m.quota.daily_cap is not None
    domains = {d.pattern for d in m.permissions.domains}
    assert domains == {"openlibrary.org", "covers.openlibrary.org", "*.archive.org"}
    assert m.permissions.lan_targets == []
    assert m.entity_types == {"edition", "volume"}  # an ISBN never designates a Work


@pytest.mark.req("EF-BIB-013", "ENF-LEG-003")
def test_the_manifest_file_is_the_one_in_code() -> None:
    on_disk = json.loads((REPO / "sources" / "meta_openlibrary" / "manifest.json").read_text())
    assert on_disk == json.loads(build_manifest().model_dump_json())


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_the_age_is_never_a_field_of_the_provider() -> None:
    """Nothing about an age is among the fields it may touch, whatever the record says."""
    m = build_manifest()
    assert not {f for f in m.touched_fields if "age" in f or "rating" in f}
    hostile = record(age="18", age_rating="all", rating="0+", subjects=[{"name": "Erotica"}])
    out = run(OpenLibraryProvider().identify(fetcher({f"ISBN:{ISBN}": hostile}), TARGET,
                                             MatchHints()))  # fmt: skip
    assert {p.field for p in out.proposals} <= m.touched_fields
    assert not [p for p in out.proposals if "age" in p.field or "rating" in p.field]
    (age,) = [r for r in out.relations if r.kind == "age_suggestion"]
    assert age.data == {"min_age": 18, "x": False} and age.subject == TARGET  # a suggestion


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_a_juvenile_subject_never_lowers_an_age() -> None:
    body = record(subjects=[{"name": "Juvenile fiction"}, {"name": "Children's stories"}])
    out = run(OpenLibraryProvider().identify(fetcher({f"ISBN:{ISBN}": body}), TARGET, MatchHints()))
    assert [r for r in out.relations if r.kind == "age_suggestion"] == []


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_proposals_carry_the_attribution_licence_and_a_clean_source_url() -> None:
    out = run(
        OpenLibraryProvider().identify(fetcher({f"ISBN:{ISBN}": record()}), TARGET, MatchHints())
    )
    fields = {p.field: p for p in out.proposals}
    assert fields["title"].value == "Synthetic title"
    assert fields["contributors"].value == ["Ada Example", "Bo Sample"]
    assert fields["publisher"].value == "Example Press"
    assert fields["published_on"].value == "2001"
    assert all(p.entity_type == "edition" for p in out.proposals)  # the Edition, not the Work
    for p in out.proposals:
        prov = p.provenance
        assert prov.provider == "openlibrary" and prov.attribution and "Open Library" in prov.attribution
        assert prov.source_url == f"https://{API_HOST}/books/{OL_ID}" and "?" not in prov.source_url
        assert prov.licence == "CC0-1.0"
        assert 0 < p.confidence <= 1


@pytest.mark.req("EF-BIB-013")
def test_an_unknown_isbn_proposes_nothing_and_a_down_server_raises_unavailable() -> None:
    assert run(OpenLibraryProvider().identify(fetcher({}), TARGET, MatchHints())).proposals == []
    for status in (500, 502, 503, 429):
        with pytest.raises(ProviderUnavailable):
            run(OpenLibraryProvider().identify(fetcher({}, status), TARGET, MatchHints()))


@pytest.mark.req("EF-BIB-013", "ENF-SEC-003")
def test_a_garbled_answer_is_unavailable_not_a_crash_and_values_are_typed() -> None:
    garbled = FakeFetcher({("GET", BOOKS): Response(status=200, body=b"<html>oops")})
    with pytest.raises(ProviderUnavailable):
        run(OpenLibraryProvider().identify(garbled, TARGET, MatchHints()))
    odd = record(title=["not", "a", "string"], authors="Ada", publishers=[{"name": 3}],
                 subjects=None, publish_date={"a": 1})  # fmt: skip
    out = run(OpenLibraryProvider().identify(fetcher({f"ISBN:{ISBN}": odd}), TARGET, MatchHints()))
    assert out.proposals == []  # nothing of the wrong type is proposed


@pytest.mark.req("EF-BIB-013", "ENF-SEC-003")
def test_the_cover_is_a_reference_the_host_decodes_never_bytes() -> None:
    cover = run(OpenLibraryProvider().get_cover(fetcher({}), TARGET))
    assert cover is not None
    assert cover.url == f"https://{COVER_HOST}/b/isbn/{ISBN}-L.jpg?default=false"
    assert cover.headers == {} and cover.body is None and cover.method == "GET"
    assert run(OpenLibraryProvider().get_cover(fetcher({}), EntityRef(
        entity_type="edition", ids={"openlibrary": OL_ID}))) is None  # no ISBN: no guess


@pytest.mark.req("EF-BIB-013")
def test_url_and_identifier_map_both_ways() -> None:
    p = OpenLibraryProvider()
    assert p.url_for({"isbn13": ISBN}) == f"https://{API_HOST}/isbn/{ISBN}"
    assert p.ids_from_url(f"https://{API_HOST}/isbn/{ISBN}") == {"isbn13": ISBN}
    assert p.url_for({"openlibrary": OL_ID}) == f"https://{API_HOST}/books/{OL_ID}"
    assert p.ids_from_url(f"https://{API_HOST}/books/{OL_ID}/Some_title") == {"openlibrary": OL_ID}
    for bad in ("https://evil.example/isbn/" + ISBN, f"https://{API_HOST}/isbn/not-an-isbn",
                f"http://{API_HOST}/isbn/{ISBN}", f"https://{API_HOST}.evil.example/isbn/{ISBN}"):
        assert p.ids_from_url(bad) is None
    assert p.url_for({}) is None


@pytest.mark.req("EF-SRC-033", "ENF-SEC-001")
def test_the_adapter_imports_only_the_sdk_and_nothing_that_decodes_or_connects() -> None:
    """It receives its Fetcher, never imports one; it never opens an image."""
    root = Path(package.__file__).parent
    imported: set[str] = set()
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module)
    own = {m for m in imported if m.startswith("shelfhold.")}
    assert own and all(m.startswith("shelfhold.sdk") for m in own), own
    forbidden = ("PIL", "httpx", "httpx2", "socket", "requests", "urllib.request", "aiohttp")
    assert not {m for m in imported if m.split(".")[0] in forbidden or m in forbidden}, imported
