"""M4 L23 (EF-BIB-032): « Soutenir l'auteur » — official links with provenance, never affiliated,
https only, and the proposal (never the act) of pausing a community binding.

Everything is fabricated: invented hosts under `example.org`, a cassette of an invented AniList.
Nothing leaves the machine. The attacks on the HTTP surface (child, member, key, IDOR) are in
`test_l23_support_author_http.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.catalog.application.enrich import EnrichService
from shelfhold.catalog.application.official_links import (
    MAX_LINKS_PER_PROVIDER,
    OfficialLink,
    is_safe_url,
    official_links,
    pause_community_binding,
    read_official_links,
    sanitize_url,
    store_official_links,
    strip_affiliate_params,
    suggest_pausing_community_binding,
)
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.imaging import WorkerLimits
from shelfhold.infrastructure.schema import acquisition, catalog, metadata
from shelfhold.plugin_host.adapters.native.anilist import AniListProvider, build_manifest
from shelfhold.sdk.metadata import (
    EntityRef,
    IdentifyResult,
    MatchHints,
    Provenance,
    RelationProposal,
)
from tests.fakes.anilist.server import TITLE, fake_anilist
from tests.fakes_clock import ManualClock
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_work,
)
from tests.l21b_bench import FakeTime, run, worker

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
EDITION = "01HZZZZZZZZZZZZZZZZZZZZZZA"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_db_engine(tmp_path / "t.db")
    metadata.create_all(eng)
    yield eng
    eng.dispose()


def provenance(provider: str = "anilist") -> Provenance:
    return Provenance(
        provider=provider,
        fetched_at=NOW,
        source_url="https://anilist.co/manga/1",
        attribution="Data from AniList",
    )


def relation(
    url: Any,
    *,
    provider: str = "anilist",
    kind: str = "official_link",
    entity_id: str | None = EDITION,
    entity_type: str = "edition",
    data_kind: str | None = None,
    **data: Any,
) -> RelationProposal:
    return RelationProposal(
        kind=kind,  # type: ignore[arg-type]
        subject=EntityRef(entity_type=entity_type, entity_id=entity_id),  # type: ignore[arg-type]
        data={"url": url, **data, **({"kind": data_kind} if data_kind else {})},
        confidence=0.9,
        provenance=provenance(provider),
    )


# -- the criterion of the card -------------------------------------------------------------------


@pytest.mark.req("EF-BIB-032")
def test_support_author_official_links_with_provenance() -> None:
    result = IdentifyResult(relations=[relation("https://example.org/read")])
    links = official_links(result)
    assert isinstance(links[0], OfficialLink)
    assert links[0].url == "https://example.org/read"
    assert links[0].provider == "anilist"
    assert links[0].provenance is not None
    assert links[0].provenance.provider == "anilist"
    assert links[0].fetched_at == NOW  # when the provider said so


@pytest.mark.req("EF-BIB-032")
def test_no_affiliate_links() -> None:
    cleaned = strip_affiliate_params("https://example.org/buy?tag=aff&x=1&utm_source=news")
    assert cleaned == "https://example.org/buy?x=1"
    assert sanitize_url("https://example.org/x?aff=1") == "https://example.org/x"
    # case, encoded names, every utm_*, the fragment: nothing that tracks or pays a referrer stays
    assert sanitize_url("https://example.org/x?TAG=a&%74ag=b&Utm_Custom=c&REF=d#frag") == (
        "https://example.org/x"
    )
    assert sanitize_url("https://example.org/p?id=7&gclid=z&fbclid=y&page=2") == (
        "https://example.org/p?id=7&page=2"
    )
    # no residue in any form
    for url in ("https://example.org/x?tag=a", "https://example.org/x?ascsubtag=a&linkCode=b"):
        out = sanitize_url(url)
        assert out is not None and "?" not in out and "tag" not in out.lower()


@pytest.mark.req("EF-BIB-032")
def test_official_edition_in_household_language_suggests_pausing_binding(engine: Engine) -> None:
    with engine.begin() as c:
        work = add_work(c)
        official = add_edition(c, work, lang="fr", is_official=True, publisher="Official Pub")
        community_edition = add_edition(
            c, work, lang="fr", is_official=False, publisher="Community Pub"
        )
        binding = add_binding(c, work, community_edition, add_source_item(c))
        assert suggest_pausing_community_binding(c, edition_id=official) == [binding]
        # a proposal only: nothing was paused by asking
        active = c.execute(
            sa.select(acquisition.source_binding.c.active).where(
                acquisition.source_binding.c.id == binding
            )
        ).scalar_one()
        assert active == 1


@pytest.mark.req("EF-BIB-032")
def test_official_link_https_only_and_rel_noopener() -> None:
    assert sanitize_url("http://example.org/x") is None
    assert sanitize_url("javascript:alert(1)") is None
    assert sanitize_url("data:text/html;base64,AAAA") is None
    assert sanitize_url("https://example.org/x") == "https://example.org/x"
    # the `rel="noopener noreferrer"` of the screen is proved in SupportAuthor.test.ts


# -- the URL of a third party (ADR-0047) ---------------------------------------------------------


@pytest.mark.req("EF-BIB-032")
@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,x",
        "vbscript:x",
        "file:///etc/passwd",
        "ftp://example.org/x",
        "http://example.org/x",
        "//example.org/x",
        "https:example.org",
        "https:///x",
        "https://",
        "https://user:pw@example.org/x",  # credentials
        "https://example.org@evil.example.net/x",  # the host is not the one it looks like
        "https://localhost/x",
        "https://intranet/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://example.org/x y",
        " https://example.org/x",
        "https://example.org/x\n",
        "https://example.org/\x00",
        "https://example.org:notaport/x",
        "https://example.org/" + "a" * 3000,
        "",
        None,
        42,
        b"https://example.org",
        ["https://example.org"],
    ],
)
def test_unsafe_urls_are_refused(url: object) -> None:
    assert is_safe_url(url) is False
    assert sanitize_url(url) is None


@pytest.mark.req("EF-BIB-032")
def test_uppercase_scheme_and_port_are_normal() -> None:
    assert sanitize_url("HTTPS://Example.org:8443/x?id=1") == "https://Example.org:8443/x?id=1"


# -- what a provider hands over ------------------------------------------------------------------


@pytest.mark.req("EF-BIB-032")
def test_only_closed_list_providers_and_only_links_about_the_edition() -> None:
    result = IdentifyResult(
        relations=[
            relation("https://example.org/a"),
            relation("https://example.org/b", provider="mangaupdates"),
            relation("https://example.org/c", provider="openlibrary"),  # not a link provider
            relation("https://example.org/d", correspondences={"viaf": "1"}),  # an identifier page
            relation("https://example.org/e", entity_id="01HZZZZZZZZZZZZZZZZZZZZZZB"),  # not ours
            relation("https://example.org/f", entity_type="work"),
            relation("javascript:alert(1)"),
            relation("https://example.org/h", kind="age_suggestion"),
        ]
    )
    wanted = {link.url for link in official_links(result, edition_id=EDITION)}
    assert wanted == {"https://example.org/a", "https://example.org/b"}
    # without an Edition asked, the link of another Edition is still a clean link
    assert "https://example.org/e" in {link.url for link in official_links(result)}


@pytest.mark.req("EF-BIB-032")
def test_links_are_deduplicated_capped_and_normalised() -> None:
    many = [relation(f"https://example.org/{i}") for i in range(MAX_LINKS_PER_PROVIDER + 5)]
    twice = [relation("https://example.org/same?tag=a"), relation("https://example.org/same?ref=b")]
    links = official_links(IdentifyResult(relations=[*many, *twice]))
    assert len(links) == MAX_LINKS_PER_PROVIDER  # the cap, the duplicate counted once
    assert len({link.url for link in links}) == len(links)
    odd = official_links(
        IdentifyResult(
            relations=[
                relation("https://example.org/k1", kind="licensed_in", lang="fr"),
                relation("https://example.org/k2", kind="official_link", lang="not a tag!"),
                relation("https://example.org/k3", data_kind="buy"),
                relation("https://example.org/k4", data_kind="evil"),
            ]
        )
    )
    by_url = {link.url: link for link in odd}
    assert by_url["https://example.org/k1"].kind == "licensed_in"
    assert by_url["https://example.org/k1"].lang == "fr"
    assert by_url["https://example.org/k2"].lang == "und"
    assert by_url["https://example.org/k3"].kind == "buy"
    assert by_url["https://example.org/k4"].kind == "official_read"


# -- what is stored ------------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-032")
def test_store_replaces_one_providers_rows_and_keeps_the_others(engine: Engine) -> None:
    with engine.begin() as c:
        edition = add_edition(c, add_work(c))
        first = official_links(IdentifyResult(relations=[relation("https://example.org/old")]))
        other = official_links(
            IdentifyResult(relations=[relation("https://example.org/mu", provider="mangaupdates")])
        )
        store_official_links(c, edition, first, provider="anilist", now=NOW)
        store_official_links(c, edition, other, provider="mangaupdates", now=NOW)
        fresh = official_links(IdentifyResult(relations=[relation("https://example.org/new")]))
        assert store_official_links(c, edition, fresh, provider="anilist", now=NOW) == 1
        urls = {(link.provider, link.url) for link in read_official_links(c, edition)}
        assert urls == {
            ("anilist", "https://example.org/new"),
            ("mangaupdates", "https://example.org/mu"),
        }
        # a provider cannot store a link under another provider's name
        assert store_official_links(c, edition, fresh, provider="mangaupdates", now=NOW) == 0


@pytest.mark.req("EF-BIB-032")
def test_the_database_refuses_a_link_that_is_not_https(engine: Engine) -> None:
    with engine.begin() as c:
        edition = add_edition(c, add_work(c))
    for url in ("javascript:alert(1)", "http://example.org/x", "data:text/html,x", ""):
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as c:
            c.execute(
                sa.insert(catalog.external_link).values(
                    id="01HZZZZZZZZZZZZZZZZZZZZZZA", entity_type="edition", entity_id=edition,
                    url=url, provider="anilist", fetched_at=NOW,
                )
            )  # fmt: skip


@pytest.mark.req("EF-BIB-032")
def test_a_row_written_by_another_door_is_cleaned_on_the_way_out(engine: Engine) -> None:
    with engine.begin() as c:
        edition = add_edition(c, add_work(c))
        c.execute(
            sa.insert(catalog.external_link).values(
                id="01HZZZZZZZZZZZZZZZZZZZZZZA", entity_type="edition", entity_id=edition,
                url="https://example.org/x?tag=aff&utm_source=n", provider="anilist",
                fetched_at=NOW,
            )
        )  # fmt: skip
        c.execute(
            sa.insert(catalog.external_link).values(
                id="01HZZZZZZZZZZZZZZZZZZZZZZB", entity_type="edition", entity_id=edition,
                url="https://user:pw@example.org/x", provider="anilist", fetched_at=NOW,
            )
        )  # fmt: skip
        assert [link.url for link in read_official_links(c, edition)] == ["https://example.org/x"]


# -- the proposal to pause, never the act --------------------------------------------------------


def _world(
    c: sa.Connection, *, official_lang: str = "fr", community_lang: str = "fr"
) -> dict[str, str]:
    work = add_work(c)
    official = add_edition(c, work, lang=official_lang, is_official=True, publisher="Official")
    community = add_edition(c, work, lang=community_lang, is_official=False, publisher="Community")
    return {
        "work": work, "official": official, "community": community,
        "binding": add_binding(c, work, community, add_source_item(c, remote_id=f"s-{community}")),
    }  # fmt: skip


@pytest.mark.req("EF-BIB-032")
def test_no_proposal_when_the_official_edition_is_in_another_language(engine: Engine) -> None:
    with engine.begin() as c:
        w = _world(c, official_lang="en", community_lang="fr")
        assert suggest_pausing_community_binding(c, edition_id=w["official"]) == []


@pytest.mark.req("EF-BIB-032")
def test_no_proposal_for_a_paused_foreign_or_official_binding(engine: Engine) -> None:
    with engine.begin() as c:
        w = _world(c)
        other = _world(c)  # another Work: never named by this one
        official_binding = add_binding(
            c, w["work"], w["official"], add_source_item(c, remote_id="official")
        )
        assert official_binding not in suggest_pausing_community_binding(
            c, edition_id=w["official"]
        )
        assert other["binding"] not in suggest_pausing_community_binding(
            c, edition_id=w["official"]
        )
        c.execute(
            sa.update(acquisition.source_binding)
            .where(acquisition.source_binding.c.id == w["binding"])
            .values(active=False, disabled_reason="manual")
        )
        assert suggest_pausing_community_binding(c, edition_id=w["official"]) == []
        # a community Edition is not an official one: it proposes nothing about itself
        assert suggest_pausing_community_binding(c, edition_id=w["community"]) == []
        assert suggest_pausing_community_binding(c, edition_id="01HZZZZZZZZZZZZZZZZZZZZZZZ") == []


@pytest.mark.req("EF-BIB-032")
def test_pausing_is_the_admins_act_and_only_for_a_proposed_binding(engine: Engine) -> None:
    with engine.begin() as c:
        w = _world(c)
        other = _world(c)
        assert pause_community_binding(c, w["community"], other["binding"]) is False  # IDOR
        assert pause_community_binding(c, w["community"], "01HZZZZZZZZZZZZZZZZZZZZZZZ") is False
        assert pause_community_binding(c, w["community"], w["binding"]) is True
        row = c.execute(
            sa.select(
                acquisition.source_binding.c.active, acquisition.source_binding.c.disabled_reason
            )
            .where(acquisition.source_binding.c.id == w["binding"])
        ).one()  # fmt: skip
        assert (row.active, row.disabled_reason) == (0, "manual")
        assert pause_community_binding(c, w["community"], w["binding"]) is False  # nothing twice
        untouched = c.execute(
            sa.select(acquisition.source_binding.c.active).where(
                acquisition.source_binding.c.id == other["binding"]
            )
        ).scalar_one()
        assert untouched == 1


# -- the enrichment stores the links -------------------------------------------------------------


class _Channels:
    def channel_names(self, connection: sa.Connection) -> list[str]:
        return ["ntfy"]


@pytest.mark.req("EF-BIB-032")
def test_enrichment_by_the_provider_stores_the_links_with_their_provenance(
    engine: Engine, tmp_path: Path
) -> None:
    clock = ManualClock(NOW)
    service = EnrichService(
        engine, clock, EventBus(clock, channels=_Channels()), data_dir=tmp_path / "d",
        limits=WorkerLimits(network_namespace=False),
    )  # fmt: skip
    with engine.begin() as c:
        edition = add_edition(c, add_work(c))
    time = FakeTime()
    fetcher = worker(build_manifest(), fake_anilist("search_nominal", clock=time.now), time)
    report = run(
        service.enrich(
            AniListProvider(), fetcher, edition_id=edition, hints=MatchHints(title=TITLE)
        )
    )
    assert report.outcome == "enriched"
    with engine.connect() as c:
        (link,) = read_official_links(c, edition)
        kept = c.execute(sa.select(catalog.external_link)).mappings().one()
    assert link.url == "https://publisher.example.org/where-the-moon"
    assert (kept["provider"], kept["entity_id"], kept["kind"]) == (
        "anilist",
        edition,
        "official_read",
    )
    assert kept["fetched_at"] is not None  # the provenance: who, and when
