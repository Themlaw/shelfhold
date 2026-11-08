"""Suwayomi write capabilities (M1b L18): follow/unfollow, the Shelfhold category, release_claim,
instance fingerprint. Against the simulated server only; it referees every write it receives."""

import asyncio
import re
import time
from collections.abc import Awaitable

import pytest

from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.plugin_host.adapters.meta.suwayomi import queries as q
from shelfhold.sdk.connected import ConnectedCapability
from shelfhold.sdk.refs import SeriesRef, UnitRef
from shelfhold.sdk.source import TemporarilyUnavailable
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


def follow(
    service: SuwayomiService, f: FakeSuwayomiFetcher, ref: SeriesRef, on: bool = True, **kw: bool
) -> None:
    """As the cycle does: the fingerprint is read first, then the series is followed."""
    run(service.instance_fingerprint(f))
    run(service.set_follow(f, ref, on, **kw))


def sent(server: FakeSuwayomi, field: str) -> int:
    return sum(1 for r in server.requests if field in r.fields)


def manga(server: FakeSuwayomi, url: str) -> dict[str, object]:
    return next(m for m in server.state["mangas"] if m["url"] == url)


@pytest.mark.req("EF-SRC-029")
def test_follow_creates_single_entry_in_shelfhold_category_no_autodownload() -> None:
    service, f, server = make()
    assert not manga(server, "/series-gamma")["inLibrary"]
    follow(service, f, GAMMA)
    (category,) = server.categories
    assert category["name"] == "Shelfhold"
    assert category["includeInDownload"] == "EXCLUDE"  # no automatic download (EF-SRC-009)
    assert category["includeInUpdate"] == "UNSET"  # Suwayomi still refreshes the followed series
    assert manga(server, "/series-gamma")["inLibrary"] is True
    assert server.categories_of("/series-gamma") == [category["id"]]
    assert sent(server, "enqueueChapterDownload") == 0  # nothing is downloaded by following
    assert sum(1 for m in server.state["mangas"] if m["inLibrary"]) == 3
    assert server.violations == []


@pytest.mark.req("EF-SRC-029")
def test_follow_existing_series_links_without_duplicate() -> None:
    service, f, server = make()
    before = len(server.state["mangas"])
    follow(service, f, ALPHA)  # already in the library of Suwayomi
    assert len(server.state["mangas"]) == before
    assert sent(server, "updateManga") == 0 and sent(server, "createCategory") == 0
    assert server.categories_of("/series-alpha") == []  # the user's own organisation is kept
    assert {r.title for r in run(service.followed_series(f))} >= {"Series Alpha"}
    assert server.violations == []


@pytest.mark.req("EF-SRC-029")
def test_unfollow_keeps_suwayomi_series_unless_explicit() -> None:
    service, f, server = make()
    follow(service, f, GAMMA)
    follow(service, f, GAMMA, False)
    assert manga(server, "/series-gamma")["inLibrary"] is True  # still in Suwayomi
    assert server.categories_of("/series-gamma") == []  # but no longer held by Shelfhold
    follow(service, f, GAMMA, False, remove_upstream=True)
    assert manga(server, "/series-gamma")["inLibrary"] is False  # only on explicit request
    assert server.violations == []


@pytest.mark.req("EF-SRC-029")
def test_follow_unfollow_idempotent() -> None:
    service, f, server = make()
    for _ in range(3):
        follow(service, f, GAMMA)
    assert len(server.categories) == 1  # the category is never duplicated
    assert server.categories_of("/series-gamma") == [server.categories[0]["id"]]
    for _ in range(3):
        follow(service, f, GAMMA, False)
    follow(service, f, GAMMA)
    assert len(server.categories) == 1 and sent(server, "createCategory") == 1
    assert server.violations == []


@pytest.mark.req("EF-SRC-029")
def test_an_existing_shelfhold_category_is_reused_not_recreated() -> None:
    service, f, server = make()
    ident = server.add_category("Shelfhold", include_in_download="EXCLUDE")
    follow(service, f, GAMMA)
    assert sent(server, "createCategory") == 0 and server.categories_of("/series-gamma") == [ident]


@pytest.mark.req("EF-SRC-029")
def test_include_in_update_is_never_written() -> None:
    """Excluding the category from the updates would stop the refresh of every followed series:
    the simulated server refuses it on `createCategory` and on `updateCategory`."""
    service, f, server = make()
    follow(service, f, GAMMA)
    assert server.violations == []  # the adapter itself never sends it
    run(
        f.post(
            f"{BASE_URL}/api/graphql",
            json={
                "query": (
                    'mutation { createCategory(input: {name: "x", includeInUpdate: EXCLUDE}) '
                    "{ category { id } } }"
                )
            },
        )
    )
    run(
        f.post(
            f"{BASE_URL}/api/graphql",
            json={
                "query": (
                    "mutation { updateCategory(input: {id: 1, patch: {includeInUpdate: EXCLUDE}}) "
                    "{ category { id } } }"
                )
            },
        )
    )
    assert len(server.violations) == 2
    writes = [r.body.decode() for r in server.requests[:-2] if b"mutation" in r.body]
    assert writes and not any("includeInUpdate" in w for w in writes)  # reads may name it


@pytest.mark.req("EF-SRC-029")
def test_an_existing_shelfhold_category_not_excluded_from_download_is_set_once() -> None:
    service, f, server = make()
    server.add_category("Shelfhold")  # made by hand: nothing excludes it from the download
    follow(service, f, GAMMA)
    follow(service, f, GAMMA, False)
    follow(service, f, GAMMA, False, remove_upstream=True)
    follow(service, f, GAMMA)
    assert sent(server, "createCategory") == 0 and sent(server, "updateCategory") == 1
    assert server.categories[0]["includeInDownload"] == "EXCLUDE"
    assert server.categories[0]["includeInUpdate"] == "UNSET" and server.violations == []


@pytest.mark.req("EF-SRC-036")
def test_no_follow_recreates_the_category_before_the_fingerprint_was_probed() -> None:
    service, f, server = make()
    with pytest.raises(TemporarilyUnavailable):
        run(service.set_follow(f, GAMMA, True))  # no probe yet
    assert server.categories == [] and sent(server, "createCategory") == 0
    follow(service, f, GAMMA)  # probed, then followed
    assert len(server.categories) == 1
    server.reinstall(wipe=True)  # a new installation, a long time after the last probe
    service._probed_at = time.monotonic() - 10_000  # type: ignore[attr-defined]
    with pytest.raises(TemporarilyUnavailable):
        run(service.set_follow(f, GAMMA, True))
    assert server.categories == []  # nothing was created in the new installation


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
        "updateCategory", "updateMangaCategories", "deleteDownloadedChapter",
    }  # fmt: skip
    _service, f, server = make()
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
    follow(service, f, GAMMA)
    follow(service, f, GAMMA, False, remove_upstream=True)
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
    follow(service, f, GAMMA)
    first = run(service.instance_fingerprint(f))
    assert first and run(service.instance_fingerprint(f)) == first  # stable
    follow(service, f, ALPHA)
    follow(service, f, GAMMA, False, remove_upstream=True)
    assert run(service.instance_fingerprint(f)) == first  # following does not move it
    server.reinstall()  # renumbered ids, same installation of the upstream sources
    assert run(SuwayomiService(BASE_URL).instance_fingerprint(f)) == first
    server.reinstall(wipe=True)  # a new database
    assert run(SuwayomiService(BASE_URL).instance_fingerprint(f)) == ""
    assert server.violations == []


@pytest.mark.req("EF-SRC-033")
def test_the_upstream_erasure_has_a_single_sender() -> None:
    """The name-based reachability analysis cannot tell `release` of a service from any other
    `release`, so this pins the guarantee: the erasure document is used by `release` and nowhere
    else in production (L16 reaches it only through the capability, after its guards)."""
    from pathlib import Path

    import shelfhold

    root = Path(shelfhold.__file__).parent
    users = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "DELETE_DOWNLOADED_CHAPTER" in p.read_text("utf-8")
    ]
    assert sorted(users) == [
        "plugin_host/adapters/meta/suwayomi/queries.py",
        "plugin_host/adapters/meta/suwayomi/service.py",
    ]
    service = (root / "plugin_host/adapters/meta/suwayomi/service.py").read_text("utf-8")
    assert service.count("q.DELETE_DOWNLOADED_CHAPTER") == 1
