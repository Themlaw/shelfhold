"""Contract of the pinned Suwayomi with Shelfhold's needs (spike S5, EF-SRC-008/009/025/031/036).

Real Suwayomi, local source only, synthetic CBZ, no extension, no egress (ENF-LEG-013).
Run with ``pytest -m contract`` (needs Docker); excluded from the default backend job.
"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from typing import Any

import pytest

from .harness import LOCAL_SOURCE_ID, PASSWORD, PINNED_VERSION, SYNTHETIC, USER, Bench

pytestmark = pytest.mark.contract


def _local_manga(bench: Bench) -> dict[str, dict[str, Any]]:
    data = bench.gql(
        f'mutation {{ fetchSourceManga(input: {{source: "{LOCAL_SOURCE_ID}", type: POPULAR, '
        "page: 1}) { mangas { id title url sourceId inLibrary } } }"
    )
    return {m["url"]: m for m in data["fetchSourceManga"]["mangas"]}


def _chapters(bench: Bench, manga_id: int) -> list[dict[str, Any]]:
    data = bench.gql(
        f"mutation {{ fetchChapters(input: {{mangaId: {manga_id}}}) "
        "{ chapters { id name url sourceOrder mangaId } } }"
    )
    return list(data["fetchChapters"]["chapters"])


def _chapter_a1(bench: Bench) -> dict[str, Any]:
    manga = _local_manga(bench)["Series A"]
    return next(c for c in _chapters(bench, manga["id"]) if c["url"] == "Series A/Ch 001.cbz")


# -- version, bench hygiene ---------------------------------------------------------------------
@pytest.mark.req("EF-SRC-008")
def test_about_server_graphql_and_rest(bench: Bench) -> None:
    gql = bench.gql("{ aboutServer { name version buildType } }")["aboutServer"]
    assert gql["name"] == "Suwayomi-Server"
    assert gql["version"] == PINNED_VERSION
    rest = bench.request("GET", "/api/v1/settings/about").json()
    assert rest["name"] == "Suwayomi-Server"
    assert rest["version"] == gql["version"]


@pytest.mark.req("ENF-LEG-013")
def test_bench_has_no_extension_store_and_only_the_local_source(bench: Bench) -> None:
    """The start-of-job assertion of ENF-LEG-013: empty lists, source 0 alone."""
    assert (
        bench.gql("{ extensionStores { nodes { __typename } } }")["extensionStores"]["nodes"] == []
    )
    assert bench.gql("{ extensions { nodes { pkgName } } }")["extensions"]["nodes"] == []
    sources = bench.gql("{ sources { nodes { id name } } }")["sources"]["nodes"]
    assert [s["id"] for s in sources] == [LOCAL_SOURCE_ID]


@pytest.mark.req("EF-SRC-008")
def test_unloaded_source_state(bench: Bench) -> None:
    """A source whose extension is absent is not listed; asking for it is a GraphQL error."""
    resp = bench.graphql('{ source(id: "123456789") { id name } }').json()
    assert resp.get("data") is None
    assert resp["errors"]


# -- identity (EF-SRC-036) ----------------------------------------------------------------------
@pytest.mark.req("EF-SRC-008")
def test_chapter_identity_is_source_id_plus_url(bench: Bench) -> None:
    mangas = _local_manga(bench)
    assert {"Series A", "Series B"} <= set(mangas)
    assert {m["sourceId"] for m in mangas.values()} == {LOCAL_SOURCE_ID}
    urls = {c["url"] for c in _chapters(bench, mangas["Series A"]["id"])}
    assert {"Series A/Ch 001.cbz", "Series A/Ch 002.cbz"} <= urls


@pytest.mark.req("ENF-LEG-013")
def test_container_has_no_route_out(bench: Bench) -> None:
    """ENF-LEG-013: no egress (literal IP, so no DNS). A control proves the probe itself works."""
    _, probe = bench.probe_egress()
    assert probe.returncode == 0, f"docker exec itself failed: {probe.stderr}"  # wrapper echoes rc
    rc = int(probe.stdout.rsplit("rc=", 1)[1])
    text = probe.stdout.lower()
    # 124 = `timeout` fired; 1 = bash refused with an explicit network error. 125-127 would be
    # a broken probe, not a network result.
    assert rc in {1, 124}, probe.stdout
    assert rc == 124 or re.search(r"unreachable|no route|timed out|refused", text), probe.stdout


# -- REST CBZ download (EF-SRC-009) ---------------------------------------------------------------
@pytest.mark.req("EF-SRC-008")
def test_rest_chapter_download_returns_a_cbz(bench: Bench) -> None:
    chapter = _chapter_a1(bench)
    dest = bench.seed_download("Series A", "Ch 001")
    try:
        resp = bench.request("GET", f"/api/v1/chapter/{chapter['id']}/download")
        flag = bench.gql(f"{{ chapter(id: {chapter['id']}) {{ isDownloaded }} }}")
    finally:
        dest.unlink(missing_ok=True)
    # a file placed by hand is served, but Suwayomi's own bookkeeping does not see it
    assert flag["chapter"]["isDownloaded"] is False
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/vnd.comicbook+zip"
    assert "attachment" in resp.headers["Content-Disposition"]
    assert resp.body[:2] == b"PK"
    assert resp.body == (SYNTHETIC / "comic_a_001.cbz").read_bytes()
    with zipfile.ZipFile(io.BytesIO(resp.body)) as archive:
        assert archive.testzip() is None
        assert "ComicInfo.xml" in archive.namelist()


@pytest.mark.req("EF-SRC-008")
def test_rest_download_without_downloaded_file_is_a_client_error(bench: Bench) -> None:
    """The local source is never downloaded by Suwayomi: nothing to serve until a file exists."""
    manga = _local_manga(bench)["Series B"]
    chapter = _chapters(bench, manga["id"])[0]
    assert bench.request("GET", f"/api/v1/chapter/{chapter['id']}/download").status == 400


@pytest.mark.req("EF-SRC-008")
def test_graphql_has_no_cbz_download_only_page_and_queue_operations(bench: Bench) -> None:
    """R7: no Query/Mutation field is named like an archive download; the equivalents are queue and page URLs."""
    mutations = {
        f["name"]
        for f in bench.gql('{ __type(name: "Mutation") { fields { name } } }')["__type"]["fields"]
    }
    assert {"enqueueChapterDownload", "deleteDownloadedChapter", "fetchChapterPages"} <= mutations
    queries = {
        f["name"]
        for f in bench.gql('{ __type(name: "Query") { fields { name } } }')["__type"]["fields"]
    }
    archive_like = re.compile(r"cbz|archive|downloadchapter|exportchapter", re.IGNORECASE)
    assert not {n for n in mutations | queries if archive_like.search(n)}
    chapter = _chapter_a1(bench)
    pages = bench.gql(
        f"mutation {{ fetchChapterPages(input: {{chapterId: {chapter['id']}}}) {{ pages }} }}"
    )["fetchChapterPages"]["pages"]
    assert len(pages) >= 1


# -- deletion of a downloaded chapter (B6) -------------------------------------------------------
@pytest.mark.req("EF-SRC-008")
def test_rest_delete_removes_the_downloaded_file(bench: Bench) -> None:
    chapter = _chapter_a1(bench)
    dest = bench.seed_download("Series A", "Ch 001")
    resp = bench.request(
        "DELETE", f"/api/v1/manga/{chapter['mangaId']}/chapter/{chapter['sourceOrder']}"
    )
    assert resp.status == 200
    assert not dest.exists()


@pytest.mark.req("EF-SRC-008")
def test_graphql_delete_downloaded_chapter_removes_the_file(bench: Bench) -> None:
    chapter = _chapter_a1(bench)
    dest = bench.seed_download("Series A", "Ch 001")
    bench.gql(
        f"mutation {{ deleteDownloadedChapter(input: {{id: {chapter['id']}}}) {{ chapters {{ id }} }} }}"
    )
    assert not dest.exists()
    assert bench.request("GET", f"/api/v1/chapter/{chapter['id']}/download").status == 400


# -- refresh, settings ---------------------------------------------------------------------------
@pytest.mark.req("EF-SRC-008")
def test_refresh_chapters_sees_a_new_file(bench: Bench) -> None:
    manga = _local_manga(bench)["Series A"]
    new = bench.data_dir / "local" / "Series A" / "Ch 003.cbz"
    assert "Series A/Ch 003.cbz" not in {c["url"] for c in _chapters(bench, manga["id"])}
    new.write_bytes((SYNTHETIC / "comic_a_002.cbz").read_bytes())
    new.chmod(0o666)
    try:
        assert "Series A/Ch 003.cbz" in {c["url"] for c in _chapters(bench, manga["id"])}
    finally:
        new.unlink(missing_ok=True)
        _chapters(bench, manga["id"])  # refresh again so later tests see the original state


@pytest.mark.req("EF-SRC-008")
def test_global_update_settings_are_readable(bench: Bench) -> None:
    settings = bench.gql(
        "{ settings { globalUpdateInterval updateMangas excludeCompleted excludeNotStarted "
        "excludeUnreadChapters authMode downloadAsCbz } }"
    )["settings"]
    assert settings["authMode"] == bench.mode.upper()
    assert isinstance(settings["globalUpdateInterval"], float)
    assert isinstance(settings["updateMangas"], bool)
    assert settings["downloadAsCbz"] is False


# -- library and categories (EF-SRC-029) ---------------------------------------------------------
@pytest.mark.req("EF-SRC-008")
def test_library_and_category_mutations(bench: Bench) -> None:
    manga = _local_manga(bench)["Series B"]
    category = bench.gql(
        'mutation { createCategory(input: {name: "contract"}) { category { id name } } }'
    )["createCategory"]["category"]
    cats = "{ manga { categories { nodes { name } } } }"
    try:
        added = bench.gql(
            f"mutation {{ updateManga(input: {{id: {manga['id']}, patch: {{inLibrary: true}}}}) "
            "{ manga { inLibrary } } }"
        )
        assert added["updateManga"]["manga"]["inLibrary"] is True
        assigned = bench.gql(
            f"mutation {{ updateMangaCategories(input: {{id: {manga['id']}, patch: "
            f"{{addToCategories: [{category['id']}]}}}}) {cats} }}"
        )
        nodes = assigned["updateMangaCategories"]["manga"]["categories"]["nodes"]
        assert [n["name"] for n in nodes] == ["contract"]
        removed = bench.gql(
            f"mutation {{ updateMangaCategories(input: {{id: {manga['id']}, patch: "
            f"{{removeFromCategories: [{category['id']}]}}}}) {cats} }}"
        )
        assert removed["updateMangaCategories"]["manga"]["categories"]["nodes"] == []
    finally:
        bench.gql(
            f"mutation {{ updateManga(input: {{id: {manga['id']}, patch: {{inLibrary: false}}}}) "
            "{ manga { inLibrary } } }"
        )
        bench.gql(
            f"mutation {{ deleteCategory(input: {{categoryId: {category['id']}}}) "
            "{ category { id } } }"
        )
    assert (
        bench.gql(f"{{ manga(id: {manga['id']}) {{ inLibrary }} }}")["manga"]["inLibrary"] is False
    )


# -- authentication ------------------------------------------------------------------------------
@pytest.mark.req("EF-SRC-008")
def test_wrong_credentials_are_rejected(bench: Bench) -> None:
    if bench.mode == "basic_auth":
        assert bench.request("GET", "/api/v1/settings/about", password="changed").status == 401
        assert bench.graphql("{ aboutServer { version } }", password="changed").status == 401
        assert bench.request("GET", "/api/v1/settings/about", authenticated=False).status == 401
    else:
        assert bench.request("GET", "/api/v1/settings/about", authenticated=False).status == 401
        bad = bench.graphql(
            f'mutation {{ login(input: {{username: "{USER}", '
            'password: "changed"}) { accessToken } }',
            authenticated=False,
        ).json()
        assert bad.get("data") is None or bad["data"]["login"] is None
        assert bad["errors"]


@pytest.mark.req("EF-SRC-008")
def test_ui_login_token_flow(bench: Bench) -> None:
    if bench.mode != "ui_login":
        pytest.skip("JWT flow exists only in ui_login")
    anonymous = bench.graphql("{ sources { nodes { id } } }", authenticated=False)
    assert anonymous.status == 200  # GraphQL answers 200 with an error, REST answers 401
    errors = anonymous.json()["errors"]
    assert "Unauthorized" in errors[0]["message"]
    pair = bench.login()
    refreshed = bench.gql(
        f'mutation {{ refreshToken(input: {{refreshToken: "{pair["refresh"]}"}}) {{ accessToken }} }}'
    )
    assert refreshed["refreshToken"]["accessToken"]
    assert bench.request("GET", "/api/v1/settings/about").status == 200  # Bearer on REST


@pytest.mark.req("EF-SRC-008")
def test_ui_login_rest_refuses_basic_and_about_server_is_public(bench: Bench) -> None:
    if bench.mode != "ui_login":
        pytest.skip("ui_login only")
    basic = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    refused = bench.request(
        "GET", "/api/v1/settings/about", authenticated=False, headers={"Authorization": basic}
    )
    assert refused.status == 401
    public = bench.graphql("{ aboutServer { version } }", authenticated=False)
    assert public.json()["data"]["aboutServer"]["version"] == PINNED_VERSION


# -- identity after restart (EF-SRC-036) -----------------------------------------------------------
@pytest.mark.req("EF-SRC-008")
def test_chapter_identity_is_stable_after_container_restart(bench: Bench) -> None:
    before = {
        m["url"]: (m["sourceId"], sorted((c["url"], c["id"]) for c in _chapters(bench, m["id"])))
        for m in _local_manga(bench).values()
    }
    bench.restart()
    if bench.mode == "ui_login":
        bench.login()
    after = {
        m["url"]: (m["sourceId"], sorted((c["url"], c["id"]) for c in _chapters(bench, m["id"])))
        for m in _local_manga(bench).values()
    }
    assert after == before  # (sourceId, chapter.url) unchanged; local ids even persisted
    json.dumps(after)
