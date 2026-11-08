"""Suwayomi write capabilities (M1b L18): follow/unfollow, the Shelfhold category, release_claim,
instance fingerprint. Against the simulated server only; it referees every write it receives."""

import asyncio
import re
from collections.abc import Awaitable

import pytest

from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.plugin_host.adapters.meta.suwayomi import queries as q
from shelfhold.sdk.connected import ConnectedCapability
from shelfhold.sdk.refs import SeriesRef, UnitRef
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher

GAMMA = SeriesRef(source_key="suwayomi:7003", remote_id="/series-gamma", title="Series Gamma")
ALPHA = SeriesRef(source_key="suwayomi:7001", remote_id="/series-alpha", title="Series Alpha")


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


def make() -> tuple[SuwayomiService, FakeSuwayomiFetcher, FakeSuwayomi]:
    server = FakeSuwayomi(auth_mode="none")
    return SuwayomiService(BASE_URL), FakeSuwayomiFetcher(server), server


def sent(server: FakeSuwayomi, field: str) -> int:
    return sum(1 for r in server.requests if field in r.fields)


def manga(server: FakeSuwayomi, url: str) -> dict[str, object]:
    return next(m for m in server.state["mangas"] if m["url"] == url)


@pytest.mark.req("EF-SRC-029")
def test_follow_creates_single_entry_in_shelfhold_category_no_autodownload() -> None:
    service, f, server = make()
    assert not manga(server, "/series-gamma")["inLibrary"]
    run(service.set_follow(f, GAMMA, True))
    (category,) = server.categories
    assert category["name"] == "Shelfhold" and category["includeInUpdate"] == "EXCLUDE"
    assert manga(server, "/series-gamma")["inLibrary"] is True
    assert server.categories_of("/series-gamma") == [category["id"]]
    assert sent(server, "enqueueChapterDownload") == 0  # nothing is downloaded by following
    assert sum(1 for m in server.state["mangas"] if m["inLibrary"]) == 3
    assert server.violations == []


@pytest.mark.req("EF-SRC-029")
def test_follow_existing_series_links_without_duplicate() -> None:
    service, f, server = make()
    before = len(server.state["mangas"])
    run(service.set_follow(f, ALPHA, True))  # already in the library of Suwayomi
    assert len(server.state["mangas"]) == before
    assert sent(server, "updateManga") == 0 and sent(server, "createCategory") == 0
    assert server.categories_of("/series-alpha") == []  # the user's own organisation is kept
    assert {r.title for r in run(service.followed_series(f))} >= {"Series Alpha"}
    assert server.violations == []


@pytest.mark.req("EF-SRC-029")
def test_unfollow_keeps_suwayomi_series_unless_explicit() -> None:
    service, f, server = make()
    run(service.set_follow(f, GAMMA, True))
    run(service.set_follow(f, GAMMA, False))
    assert manga(server, "/series-gamma")["inLibrary"] is True  # still in Suwayomi
    assert server.categories_of("/series-gamma") == []  # but no longer held by Shelfhold
    run(service.set_follow(f, GAMMA, False, remove_upstream=True))
    assert manga(server, "/series-gamma")["inLibrary"] is False  # only on explicit request
    assert server.violations == []


@pytest.mark.req("EF-SRC-029")
def test_follow_unfollow_idempotent() -> None:
    service, f, server = make()
    for _ in range(3):
        run(service.set_follow(f, GAMMA, True))
    assert len(server.categories) == 1  # the category is never duplicated
    assert server.categories_of("/series-gamma") == [server.categories[0]["id"]]
    for _ in range(3):
        run(service.set_follow(f, GAMMA, False))
    run(service.set_follow(f, GAMMA, True))
    assert len(server.categories) == 1 and sent(server, "createCategory") == 1
    assert server.violations == []


@pytest.mark.req("EF-SRC-029")
def test_an_existing_shelfhold_category_is_reused_not_recreated() -> None:
    service, f, server = make()
    ident = server.add_category("Shelfhold", include_in_update="EXCLUDE")
    run(service.set_follow(f, GAMMA, True))
    assert sent(server, "createCategory") == 0 and server.categories_of("/series-gamma") == [ident]


@pytest.mark.req("EF-SRC-029", "EF-SRC-033")
def test_only_mutations_verified_by_s5_are_allow_listed() -> None:
    documents = [v for k, v in vars(q).items() if k.isupper() and isinstance(v, str)]
    roots = {
        name
        for doc in documents
        if doc.startswith("mutation")
        for name in re.findall(r"\)?\s*\{\s*(\w+)\(input", doc.replace("{{", "{"))
    }
    assert roots == {
        "enqueueChapterDownload", "dequeueChapterDownload", "updateManga", "createCategory",
        "updateMangaCategories", "deleteDownloadedChapter",
    }  # fmt: skip
    service, f, server = make()
    # any other mutation is refused by the referee, and recorded
    run(f.post(f"{BASE_URL}/api/graphql", json={"query": "mutation { updateLibrary { x } }"}))
    run(f.post(f"{BASE_URL}/api/graphql", json={"query": "mutation { deleteCategory { x } }"}))
    assert len(server.violations) == 2


@pytest.mark.req("EF-SRC-033")
def test_release_claim_deletes_downloaded_chapter() -> None:
    service, f, server = make()
    assert ConnectedCapability.RELEASE_CLAIM in service.connected
    ref = ALPHA
    _, units = run(_units(service, f, ref))
    ticket = run(service.request(f, UnitRef(series=ref, unit=units[0])))
    while run(service.poll(f, ticket)) != "ready":
        pass
    chapter_id = server.local_chapter_ids()["/series-alpha/ch-001"]
    assert run(service.release(f, ticket)).released is True
    assert server.deleted == [chapter_id]
    assert run(service.poll(f, ticket)) != "ready"  # the file is gone upstream
    assert run(service.release(f, ticket)).released is False  # nothing left to erase: idempotent
    assert server.deleted == [chapter_id] and server.violations == []


@pytest.mark.req("EF-SRC-033")
def test_release_claim_erases_nothing_that_is_not_downloaded() -> None:
    service, f, server = make()
    _, units = run(_units(service, f, ALPHA))
    ticket = run(service.request(f, UnitRef(series=ALPHA, unit=units[0])))
    assert run(service.release(f, ticket)).released is False  # still queued: not a claim yet
    assert server.deleted == []


@pytest.mark.req("EF-SRC-033")
def test_nothing_but_release_claim_deletes_upstream() -> None:
    service, f, server = make()
    _, units = run(_units(service, f, ALPHA))
    run(service.set_follow(f, GAMMA, True))
    run(service.set_follow(f, GAMMA, False, remove_upstream=True))
    ticket = run(service.request(f, UnitRef(series=ALPHA, unit=units[0])))
    run(service.poll(f, ticket))
    run(service.cancel(f, ticket))
    run(service.fetch_unit(f, ALPHA, units[0]))
    run(service.instance_fingerprint(f))
    assert sent(server, "deleteDownloadedChapter") == 0


async def _units(service: SuwayomiService, f: FakeSuwayomiFetcher, ref: SeriesRef):  # type: ignore[no-untyped-def]
    return ref, await service.list_units(f, ref)


@pytest.mark.req("EF-SRC-036")
def test_instance_fingerprint_follows_the_shelfhold_category_not_the_library() -> None:
    service, f, server = make()
    assert ConnectedCapability.INSTANCE_FINGERPRINT in service.connected
    assert run(service.instance_fingerprint(f)) == ""  # no Shelfhold category yet: unknown
    run(service.set_follow(f, GAMMA, True))
    first = run(service.instance_fingerprint(f))
    assert first and run(service.instance_fingerprint(f)) == first  # stable
    run(service.set_follow(f, ALPHA, True))
    run(service.set_follow(f, GAMMA, False, remove_upstream=True))
    assert run(service.instance_fingerprint(f)) == first  # following does not move it
    server.reinstall()  # renumbered ids, same installation of the upstream sources
    assert run(SuwayomiService(BASE_URL).instance_fingerprint(f)) == first
    server.reinstall(wipe=True)  # a new database
    assert run(SuwayomiService(BASE_URL).instance_fingerprint(f)) == ""
    assert server.violations == []
