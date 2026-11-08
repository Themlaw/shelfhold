"""Review of PR #37: error classes, mode detection, handle validation, file names, caching."""

import asyncio
import json
from collections.abc import Awaitable
from typing import Any

import pytest

from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.plugin_host.authflow import AuthInjectingFetcher
from shelfhold.plugin_host.errors import (
    AuthExpired,
    AuthModeUnsupported,
    failure_class_of,
)
from shelfhold.sdk.fetcher import CookieJarView, Response
from shelfhold.sdk.models import AuthBasic
from shelfhold.sdk.refs import FileAsset, UnitRef
from shelfhold.sdk.source import AuthRequired, SourceChanged
from tests.fakes.suwayomi import BASE_URL, PASSWORD, USER, FakeSuwayomi, FakeSuwayomiFetcher


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


class Canned:
    """A Fetcher that answers every GraphQL call with one canned JSON body."""

    def __init__(self, body: Any, status: int = 200) -> None:
        self.body, self.status = body, status

    async def post(self, url: str, **kwargs: Any) -> Response:
        return Response(status=self.status, body=json.dumps(self.body).encode())

    async def get(self, url: str, **kwargs: Any) -> Response:  # pragma: no cover - unused
        raise AssertionError

    def cookies(self) -> CookieJarView:  # pragma: no cover - unused
        return CookieJarView()


def alpha(service: SuwayomiService, f: Any) -> tuple[Any, list[Any]]:
    page = run(service.search(f, "", {}, None))
    ref = next(r for r in page.items if r.title == "Series Alpha")
    return ref, run(service.list_units(f, ref))


# --- E1: a refused credential is an expired session, a missing one is a required one ----------
@pytest.mark.req("EF-SRC-023", "EF-SRC-008")
def test_review_401_with_credentials_is_auth_expired_without_is_auth_required() -> None:
    service = SuwayomiService(BASE_URL)
    server = FakeSuwayomi(auth_mode="basic_auth")
    wrong = AuthInjectingFetcher(
        FakeSuwayomiFetcher(server),
        AuthBasic(),
        {"username": USER, "password": "changed"},
        origin=BASE_URL,
    )
    with pytest.raises(AuthExpired) as expired:
        run(service.semantic_health(wrong))
    assert expired.value.code == "source.auth_expired"
    assert failure_class_of(expired.value) == "auth"

    with pytest.raises(AuthRequired) as required:
        run(service.semantic_health(FakeSuwayomiFetcher(server)))
    assert type(required.value) is AuthRequired
    assert required.value.code == "source.auth_required"
    assert PASSWORD not in str(expired.value)


# --- E2: only a real Unauthorized error counts -----------------------------------------------
@pytest.mark.req("EF-SRC-031")
@pytest.mark.parametrize(
    ("errors", "expected"),
    [
        ([{"message": "Unauthorized"}], True),
        ([{"message": " unauthorized "}], True),
        ([{"message": "x", "extensions": {"classification": "UNAUTHORIZED"}}], True),
        ([{"message": "Field 'unauthorizedAccess' is not defined"}], False),
        ([{"message": "Unauthorized access to a resource of the library"}], False),
        (["Unauthorized"], False),  # not a dict
        ([None, 3], False),
        ("Unauthorized", False),
        (None, False),
    ],
)
def test_review_unauthorized_detection_is_exact(errors: Any, expected: bool) -> None:
    assert SuwayomiService._unauthorized({"errors": errors}) is expected


@pytest.mark.req("EF-SRC-031")
def test_review_ui_login_is_detected_through_the_fallback_as_in_s5() -> None:
    """S5: in ui_login an unauthenticated GraphQL call answers 200 with `Unauthorized`; the
    reported mode is not available there, so the fallback is the real path (not verified for
    this very query)."""
    service = SuwayomiService(BASE_URL)
    canned = Canned({"data": None, "errors": [{"message": "Unauthorized"}]})
    assert run(service.auth_mode(canned)) == "ui_login"  # type: ignore[arg-type]
    with pytest.raises(AuthRequired):  # since M1b `ui_login` is driven: no credentials, no data
        run(service.search(canned, "", {}, None))  # type: ignore[arg-type]


# --- E3: tolerant of null data, strict about the mode ----------------------------------------
@pytest.mark.req("EF-SRC-008", "EF-SRC-031")
def test_review_health_tolerates_null_data() -> None:
    service = SuwayomiService(BASE_URL)
    health = run(service.semantic_health(Canned({"data": None})))  # type: ignore[arg-type]
    assert not health.ok and health.detail == "source.version_unsupported"
    with pytest.raises(SourceChanged):  # no mode reported and no Unauthorized: not guessed
        run(service.auth_mode(Canned({"data": None})))  # type: ignore[arg-type]


@pytest.mark.req("EF-SRC-031")
@pytest.mark.parametrize(
    ("reported", "accepted"),
    [
        ("NONE", True),
        ("BASIC_AUTH", True),
        ("UI_LOGIN", True),  # since M1b L20: driven by the JWT AuthFlow
        ("SIMPLE_LOGIN", False),
        ("X", False),
    ],
)
def test_review_only_none_and_basic_auth_are_accepted(reported: str, accepted: bool) -> None:
    service = SuwayomiService(BASE_URL)
    canned = Canned({"data": {"settings": {"authMode": reported}, "sources": {"nodes": []}}})
    assert run(service.auth_mode(canned)) == reported.lower()  # type: ignore[arg-type]
    if accepted:
        assert run(service.sub_sources(canned)) == []  # type: ignore[arg-type]
    else:
        with pytest.raises(AuthModeUnsupported):
            run(service.sub_sources(canned))  # type: ignore[arg-type]


# --- D1: the library is read once per cycle --------------------------------------------------
def _library_scans(server: FakeSuwayomi) -> int:
    return sum(1 for r in server.requests if r.fields == {"mangas"} and b"inLibrary" not in r.body)


@pytest.mark.req("EF-SRC-033")
def test_review_all_mangas_are_cached_for_a_cycle() -> None:
    server = FakeSuwayomi(auth_mode="none")
    f = FakeSuwayomiFetcher(server)
    service = SuwayomiService(BASE_URL)
    page = run(service.search(f, "", {}, None))
    for ref in page.items:
        run(service.list_units(f, ref))
        run(service.get_series(f, ref))
    assert _library_scans(server) == 1
    service.begin_cycle()
    run(service.list_units(f, page.items[0]))
    assert _library_scans(server) == 2


# --- I1: a cached handle must belong to the same manga ---------------------------------------
@pytest.mark.req("EF-SRC-036")
def test_review_stale_handle_of_another_manga_with_the_same_chapter_url_is_refused() -> None:
    server = FakeSuwayomi(auth_mode="none")
    f = FakeSuwayomiFetcher(server)
    service = SuwayomiService(BASE_URL)
    ref, units = alpha(service, f)
    stale = units[0]  # handle 5001
    server.reinstall()
    beta = next(m for m in server.state["mangas"] if m["url"] == "/series-beta")
    beta["chapters"].append(  # the old id 5001 now names a chapter of another series
        {"id": 5001, "url": "/series-alpha/ch-001", "name": "Other", "chapterNumber": 1.0,
         "sourceOrder": 9, "scanlator": None, "uploadDate": "0", "cbz": "comic_a_002.cbz"}
    )  # fmt: skip
    fresh = SuwayomiService(BASE_URL)
    payload = run(fresh.fetch_unit(f, ref, stale))
    assert isinstance(payload, FileAsset)
    real = next(
        c["id"]
        for m in server.state["mangas"]
        if m["url"] == "/series-alpha"
        for c in m["chapters"]
        if c["url"] == "/series-alpha/ch-001"
    )
    assert payload.request.url == f"{BASE_URL}/api/v1/chapter/{real}/download"
    assert run(f.download(payload.request)).sha256 == server.cbz_sha256_of_id(real)


# --- I2: readable, safe, bounded file names --------------------------------------------------
@pytest.mark.req("EF-SRC-008")
def test_review_filename_keeps_safe_unicode_and_is_bounded() -> None:
    server = FakeSuwayomi(auth_mode="none")
    f = FakeSuwayomiFetcher(server)
    service = SuwayomiService(BASE_URL)
    chapters = server.state["mangas"][0]["chapters"]
    chapters[0]["name"] = "Chapitre 3 \u2013 été\x00\n/..\\x: *?"
    chapters[1]["name"] = "é" * 300
    chapters[1]["chapterNumber"] = -1.0
    ref, units = alpha(service, f)

    first = run(service.fetch_unit(f, ref, units[0]))
    assert isinstance(first, FileAsset)
    assert first.filename.startswith("1 - Chapitre 3 \u2013 été")
    assert not set(first.filename) & set('/\\:*?"<>|\x00\n')
    assert first.filename.endswith(".cbz")

    second = run(service.fetch_unit(f, ref, units[1]))
    assert isinstance(second, FileAsset)
    assert len(second.filename.encode()) <= 120
    assert second.filename.encode().decode("utf-8") == second.filename  # no cut character
    assert second.filename[:8].isalnum() and second.filename.startswith(
        second.filename[:8] + " - é"
    )
    # the same name through the queue
    ticket = run(service.request(f, UnitRef(series=ref, unit=units[0])))
    while run(service.poll(f, ticket)) != "ready":
        pass
    queued = run(service.fetch(f, ticket))
    assert isinstance(queued, FileAsset) and queued.filename == first.filename


# --- L1: a local id is an integer, never text put in a path ----------------------------------
@pytest.mark.req("EF-SRC-008")
def test_review_local_id_is_an_integer_in_urls() -> None:
    service = SuwayomiService(BASE_URL)
    node = {"id": "5/../../admin", "sourceId": "7001", "url": "/x", "title": "T"}
    with pytest.raises(ValueError):
        service._ref(node)
    ok = service._ref({**node, "id": "42"})
    assert ok.cover is not None and ok.cover.url == f"{BASE_URL}/api/v1/manga/42/thumbnail"
