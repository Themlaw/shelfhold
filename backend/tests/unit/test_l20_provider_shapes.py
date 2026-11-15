"""M4 L20 (EF-BIB-013): what the two providers propose from a fabricated answer, and the shapes
they refuse (a near miss, a wrong schema, a release of another series, a non-https link)."""

from __future__ import annotations

import json

import pytest

from shelfhold.plugin_host.adapters.native.anilist import AniListProvider
from shelfhold.plugin_host.adapters.native.mangaupdates import MangaUpdatesProvider
from shelfhold.sdk.fetcher import Response
from shelfhold.sdk.metadata import EntityRef, MatchHints, ProviderUnavailable
from shelfhold.sdk.testing import FakeFetcher
from tests.fakes.anilist.server import TITLE
from tests.fakes.mangaupdates.server import SERIES_ID
from tests.unit.test_l20_providers import EDITION, run

TARGET = EntityRef(entity_type="edition", entity_id=EDITION)


ANILIST = "https://graphql.anilist.co/"
MU = "https://api.mangaupdates.com/v1"


def scripted(*answers: tuple[str, object, int]) -> FakeFetcher:
    return FakeFetcher({
        ("POST", url): Response(status=status, body=json.dumps(body).encode())
        for url, body, status in answers
    })  # fmt: skip


def answer(body: object, status: int = 200) -> FakeFetcher:
    return scripted((ANILIST, body, status))


def media(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "siteUrl": "https://anilist.co/manga/900001",
        "title": {"romaji": "Tsuki", "english": TITLE, "native": None},
        "description": "A <b>synthetic</b> summary.",
        "startDate": {"year": 2019, "month": 4, "day": None},
        "genres": ["Fantasy"],
        "isAdult": True,
        "staff": {"edges": [{"node": {"name": {"full": "Ada Example"}}}] * 2},
        "externalLinks": [{"url": "https://publisher.example.org/x"}, {"url": "http://plain"}],
    }
    return {**base, **extra}


def anilist(items: list[object], status: int = 200) -> FakeFetcher:
    return answer({"data": {"Page": {"media": items}}}, status)


def ask_anilist(fetcher: FakeFetcher, title: str | None = TITLE):
    return run(AniListProvider().identify(fetcher, TARGET, MatchHints(title=title)))


@pytest.mark.req("EF-BIB-013")
def test_anilist_proposals_carry_provenance_and_only_safe_relations() -> None:
    result = ask_anilist(anilist([media()]))
    fields = {p.field: p.value for p in result.proposals}
    assert fields == {
        "title": TITLE, "description": "A synthetic summary.", "published_on": "2019-04",
        "subjects": ["Fantasy"], "contributors": ["Ada Example"],
    }  # fmt: skip
    assert {p.provenance.attribution for p in result.proposals} == {
        "Data from AniList (anilist.co)"
    }
    kinds = [(r.kind, r.data) for r in result.relations]
    assert ("official_link", {"url": "https://publisher.example.org/x"}) in kinds  # https only
    assert not any(d.get("url") == "http://plain" for _, d in kinds)
    assert ("age_suggestion", {"min_age": 18, "x": False}) in kinds  # a suggestion, never a rating


@pytest.mark.req("EF-BIB-013")
def test_anilist_never_proposes_a_near_miss_nor_without_a_title() -> None:
    assert ask_anilist(anilist([media(title={"english": "Another Title"})])).proposals == []
    assert ask_anilist(anilist([media()]), title=None).proposals == []
    assert ask_anilist(anilist([]), title=TITLE).proposals == []


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_anilist_a_refusal_or_a_wrong_shape_is_the_provider_being_down() -> None:
    for status in (429, 503):
        with pytest.raises(ProviderUnavailable):
            ask_anilist(anilist([], status))
    with pytest.raises(ProviderUnavailable):
        ask_anilist(answer({"unexpected": True}))
    with pytest.raises(ProviderUnavailable):
        ask_anilist(answer({"data": {"Page": {"media": "no"}}}))
    assert ask_anilist(anilist([], 404)).proposals == []


@pytest.mark.req("EF-BIB-013")
def test_anilist_and_mangaupdates_addresses_round_trip_exactly() -> None:
    ani, mu = AniListProvider(), MangaUpdatesProvider()
    assert ani.ids_from_url(ani.url_for({"anilist": "900001"}) or "") == {"anilist": "900001"}
    assert (
        ani.url_for({"anilist": "x"}) is None and ani.ids_from_url("https://example.org/") is None
    )
    assert mu.ids_from_url(mu.url_for({"mangaupdates": SERIES_ID}) or "") == {
        "mangaupdates": SERIES_ID
    }
    assert mu.url_for({"mangaupdates": "abc"}) is None and mu.ids_from_url("https://x.org/") is None
    assert run(ani.get_cover(FakeFetcher(), TARGET)) is None
    assert run(mu.get_cover(FakeFetcher(), TARGET)) is None
    for provider in (ani, mu):
        with pytest.raises(NotImplementedError):
            run(provider.refresh(FakeFetcher(), None))
    with pytest.raises(NotImplementedError):
        run(mu.login(FakeFetcher(), "oauth", {}))


def mu_fetcher(releases: object, status: int = 200) -> FakeFetcher:
    hit = {
        "record": {
            "series_id": int(SERIES_ID),
            "title": TITLE,
            "year": "2019",
            "description": "<i>Summary</i>",
            "genres": [{"genre": "Fantasy"}],
        }
    }
    return scripted((f"{MU}/series/search", {"results": [hit]}, 200),
                    (f"{MU}/releases/search", releases, status))  # fmt: skip


def release(day: str, series: str = SERIES_ID, chapter: str = "1") -> dict[str, object]:
    return {"record": {"release_date": day, "chapter": chapter},
            "metadata": {"series": {"series_id": int(series)}}}  # fmt: skip


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_mangaupdates_calendar_keeps_the_latest_release_of_this_series_only() -> None:
    releases = {"results": [release("2026-10-01"), release("2026-11-03", chapter="9"),
                            release("2026-12-25", series="1"), release("not a date")]}  # fmt: skip
    result = run(
        MangaUpdatesProvider().identify(
            mu_fetcher(releases),
            TARGET,
            MatchHints(title=TITLE),
        )
    )
    (calendar,) = [r for r in result.relations if r.kind == "release"]
    assert calendar.data == {"series_id": SERIES_ID, "released_on": "2026-11-03", "chapter": "9"}
    assert calendar.provenance.attribution == "Data from MangaUpdates (mangaupdates.com)"
    fields = {p.field: p.value for p in result.proposals}
    assert fields["description"] == "Summary" and fields["subjects"] == ["Fantasy"]


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_mangaupdates_refuses_near_misses_and_reports_its_outages() -> None:
    provider = MangaUpdatesProvider()
    hit = {"results": [{"record": {"series_id": 5, "title": "Another Title"}}]}
    other = scripted((f"{MU}/series/search", hit, 200))
    assert run(provider.identify(other, TARGET, MatchHints(title=TITLE))).proposals == []
    assert run(provider.identify(FakeFetcher(), TARGET, MatchHints())).proposals == []
    with pytest.raises(ProviderUnavailable):
        run(provider.identify(mu_fetcher({"results": []}, 503), TARGET, MatchHints(title=TITLE)))
    broken = scripted((f"{MU}/series/search", {}, 200))
    with pytest.raises(ProviderUnavailable):
        run(provider.identify(broken, TARGET, MatchHints(title=TITLE)))
    nothing = run(provider.identify(mu_fetcher({"results": []}), TARGET, MatchHints(title=TITLE)))
    assert [r for r in nothing.relations if r.kind == "release"] == []
