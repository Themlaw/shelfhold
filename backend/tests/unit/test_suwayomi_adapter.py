"""Suwayomi connected service (L17): EF-SRC-008, 021, 023, 031, 033, 036 against a simulated server.

Everything runs in process against `tests/fakes/suwayomi`: no socket, no third-party content.
The simulated server referees the adapter's wire behaviour (allow-listed routes and GraphQL
fields, JSON only) and records it, so these tests assert on what was actually sent.
"""

import asyncio
import io
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from shelfhold.events.waiting_state import WaitingState
from shelfhold.infrastructure.archives.safe_open import safe_open_archive
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata
from shelfhold.jobs.slots import DomainSlots
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.plugin_host.adapters.meta.suwayomi.jwt import suwayomi_jwt_fetcher
from shelfhold.plugin_host.authflow import AuthInjectingFetcher
from shelfhold.plugin_host.errors import (
    AuthExpired,
    AuthModeUnsupported,
    ServiceVersionMismatch,
    failure_class_of,
)
from shelfhold.sdk.connected import ConnectedCapability
from shelfhold.sdk.models import AuthBasic
from shelfhold.sdk.refs import FileAsset, ImageRequest, SeriesRef, UnitInfo, UnitRef
from shelfhold.sdk.source import AuthRequired
from tests.fakes.suwayomi import BASE_URL, PASSWORD, USER, FakeSuwayomi, FakeSuwayomiFetcher
from tests.fakes.suwayomi.server import ALLOWED_ROUTES
from tests.fakes_clock import ManualClock

CBZ_MIME = "application/vnd.comicbook+zip"


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


def make(
    server: FakeSuwayomi | None = None,
) -> tuple[SuwayomiService, FakeSuwayomiFetcher, FakeSuwayomi]:
    server = server or FakeSuwayomi(auth_mode="none")
    return SuwayomiService(BASE_URL), FakeSuwayomiFetcher(server), server


def series_by_title(service: SuwayomiService, f: FakeSuwayomiFetcher) -> dict[str, SeriesRef]:
    page = run(service.search(f, "", {}, None))
    return {ref.title: ref for ref in page.items}


def alpha(service: SuwayomiService, f: FakeSuwayomiFetcher) -> tuple[SeriesRef, list[UnitInfo]]:
    ref = series_by_title(service, f)["Series Alpha"]
    return ref, run(service.list_units(f, ref))


# --- EF-SRC-008: CBZ through the REST archive route, never page by page -----------------------
@pytest.mark.req("EF-SRC-008")
def test_suwayomi_downloads_cbz_via_chapter_download_endpoint() -> None:
    service, f, server = make()
    ref, units = alpha(service, f)
    first = units[0]

    # identity is upstream: (sourceId, manga.url) and sourceId + chapter.url, never the local int
    assert (ref.source_key, ref.remote_id) == ("suwayomi:7001", "/series-alpha")
    assert first.remote_unit_id == "suwayomi:7001:/series-alpha/ch-001"
    assert "5001" not in first.remote_unit_id and "101" not in ref.remote_id
    assert first.memo["fetch_handle"] == 5001  # the local integer is only a cached handle

    payload = run(service.fetch_unit(f, ref, first))
    assert isinstance(payload, FileAsset)
    assert payload.request.url == f"{BASE_URL}/api/v1/chapter/5001/download"
    assert payload.request.method == "GET"
    assert payload.mime == CBZ_MIME
    assert payload.filename.endswith(".cbz")
    # the plugin never holds the secret: the host's Fetcher injects it when it downloads
    assert not {k.lower() for k in payload.request.headers} & {"authorization", "cookie"}

    received = run(f.download(payload.request))
    assert received.sha256 == server.cbz_sha256("/series-alpha/ch-001")
    with safe_open_archive(io.BytesIO(received.content), name=payload.filename) as archive:
        assert archive.entries  # a real, audited archive


@pytest.mark.req("EF-SRC-008")
def test_never_fetches_page_by_page() -> None:
    service, f, server = make()
    ref, units = alpha(service, f)
    for unit in units:
        payload = run(service.fetch_unit(f, ref, unit))
        assert payload.kind == "file"  # not ImagePages
        ticket = run(service.request(f, UnitRef(series=ref, unit=unit)))
        while run(service.poll(f, ticket)) != "ready":
            pass
        assert run(service.fetch(f, ticket)).kind == "file"  # type: ignore[union-attr]
    paths = [r.path for r in server.requests]
    assert paths and not [p for p in paths if "/page" in p]
    assert not [r for r in server.requests if "fetchChapterPages" in r.fields]
    assert server.violations == []


# --- EF-SRC-008, EF-SRC-033: wire discipline ---------------------------------------------------
@pytest.mark.req("EF-SRC-008", "EF-SRC-033")
def test_suwayomi_adapter_sends_json_only_and_allowlisted_routes() -> None:
    service, f, server = make()
    ref, units = alpha(service, f)
    run(service.sub_sources(f))
    run(service.followed_series(f))
    run(service.semantic_health(f))
    run(service.auth_mode(f))
    run(service.set_follow(f, ref, True))
    ticket = run(service.request(f, UnitRef(series=ref, unit=units[0])))
    run(service.poll(f, ticket))
    run(service.cancel(f, ticket))
    run(service.fetch_unit(f, ref, units[0]))
    run(f.download(_asset(service, f)))

    posts = [r for r in server.requests if r.method == "POST"]
    assert posts
    assert all(r.content_type.startswith("application/json") for r in posts)
    assert all(p["json"] is not None and p["data"] is None for p in f.posts)  # never a form
    assert all(any(route.match(r.path) for route in ALLOWED_ROUTES) for r in server.requests)
    assert all(r.fields for r in posts)  # every GraphQL body is a recognised, allowed operation
    assert server.violations == []


def _asset(service: SuwayomiService, f: FakeSuwayomiFetcher) -> ImageRequest:
    ref, units = alpha(service, f)
    payload = run(service.fetch_unit(f, ref, units[0]))
    assert isinstance(payload, FileAsset)
    return payload.request


@pytest.mark.req("EF-SRC-033")
def test_csrf_mismatch_classified_version_not_auth() -> None:
    server = FakeSuwayomi(auth_mode="basic_auth", csrf_always=True)  # 401 "CSRF mismatch"
    service, f, _ = make(server)
    with pytest.raises(ServiceVersionMismatch) as raised:
        run(service.semantic_health(f))
    assert failure_class_of(raised.value) == "version"
    assert not isinstance(raised.value, AuthRequired)
    assert raised.value.code == "source.version_unsupported"

    # a plain 401 stays an authentication failure: the two are told apart
    plain, g, _ = make(FakeSuwayomi(auth_mode="basic_auth"))
    with pytest.raises(AuthRequired) as unauthorised:
        run(plain.semantic_health(g))
    assert failure_class_of(unauthorised.value) == "auth"


# --- EF-SRC-031: ui_login detected; refused in M1a, driven by the JWT AuthFlow since M1b ------
@pytest.mark.req("EF-SRC-031", "EF-SRC-008")
@pytest.mark.parametrize("m1b", [False, True], ids=["m1a_basic_credentials", "m1b_jwt_accepted"])
def test_ui_login_mode_detected_and_refused_m1a(m1b: bool) -> None:
    """M1a: `ui_login` was refused outright. M1b: it is accepted with the JWT AuthFlow; a Basic
    credential, which `ui_login` never takes, still gets nowhere (no silent partial follow)."""
    server = FakeSuwayomi(auth_mode="ui_login")
    service = SuwayomiService(BASE_URL)
    secret = {"username": USER, "password": PASSWORD}
    f: Any
    if m1b:
        f = suwayomi_jwt_fetcher(FakeSuwayomiFetcher(server), secret, origin=BASE_URL)
    else:
        f = AuthInjectingFetcher(FakeSuwayomiFetcher(server), AuthBasic(), secret, origin=BASE_URL)
    assert run(service.auth_mode(f)) == "ui_login"
    if m1b:
        assert len(run(service.followed_series(f))) == 2
        assert run(service.semantic_health(f)).ok
        return
    for attempt in (service.followed_series(f), service.search(f, "", {}, None)):
        with pytest.raises(AuthExpired):
            run(attempt)
    sent = set().union(*(r.fields for r in server.requests))
    assert sent <= {"aboutServer", "settings", "user", "mangas"}  # no account data came back


@pytest.mark.req("EF-SRC-031")
def test_unsupported_auth_mode_pauses_with_auth_mode_unsupported() -> None:
    """A mode no release drives is still refused, with the dedicated code and waiting state; the
    row `suwayomi_multi_account` of the failure matrix is proved by this one (M1b: `ui_login` is
    driven, so `source.auth_mode_unsupported` is for the modes that remain, and for an account
    that may install extensions)."""
    service, f, server = make(FakeSuwayomi(auth_mode="none"))
    server._q_settings = lambda _q, _v: {"authMode": "SAML", "downloadsPath": ""}  # type: ignore[method-assign]
    with pytest.raises(AuthModeUnsupported) as raised:
        run(service.followed_series(f))
    assert raised.value.code == "source.auth_mode_unsupported"
    assert raised.value.waiting_state is WaitingState.AUTH_MODE_UNSUPPORTED
    health = run(service.semantic_health(f))
    assert not health.ok and health.detail == "source.auth_mode_unsupported"
    assert set().union(*(r.fields for r in server.requests)) <= {"aboutServer", "settings"}


@pytest.mark.req("EF-SRC-031")
def test_basic_auth_mode_is_read_and_accepted() -> None:
    service, f, _ = make(FakeSuwayomi(auth_mode="none"))
    assert run(service.auth_mode(f)) == "none"
    assert ConnectedCapability.AUTH_MODE in service.connected


# --- EF-SRC-008: a version outside the tested range is reported -------------------------------
@pytest.mark.req("EF-SRC-008")
@pytest.mark.parametrize(
    ("version", "supported"), [("v2.4.2366", True), ("v1.1.1", False), ("v3.0.0", False)]
)
def test_suwayomi_version_out_of_range_reported(version: str, supported: bool) -> None:
    service, f, _ = make(FakeSuwayomi(auth_mode="none", version=version))
    health = run(service.semantic_health(f))
    assert health.version == version
    assert health.ok is supported
    assert health.detail == (None if supported else "source.version_unsupported")


# --- EF-SRC-021: sub-sources and the limit key of the upstream source -------------------------
@pytest.mark.req("EF-SRC-021")
def test_sub_sources_listed_dynamically() -> None:
    service, f, server = make()
    subs = {s.source_key: s for s in run(service.sub_sources(f))}
    assert set(subs) == {"suwayomi:7001", "suwayomi:7002", "suwayomi:7003"}
    assert subs["suwayomi:7001"].name == "Synthetic Source One"
    assert subs["suwayomi:7001"].lang == "en"
    assert subs["suwayomi:7003"].lang == "mul"  # "all" is multilingual, never copied
    assert subs["suwayomi:7003"].content_warning == "nsfw"
    assert subs["suwayomi:7001"].content_warning == "safe"
    assert len({s.limit_key for s in subs.values()}) == 3  # one limit per upstream source

    server.add_source("7004", "Synthetic Source Four", "de")  # nothing is cached in code
    assert len(run(service.sub_sources(f))) == 4


@pytest.mark.req("EF-SRC-021")
def test_rate_limit_keyed_by_upstream_source(tmp_path: Path) -> None:
    service, f, _ = make()
    subs = run(service.sub_sources(f))
    refs = series_by_title(service, f)
    one, two = refs["Series Alpha"], refs["Series Beta"]
    assert service.limit_key_for(one) == "suwayomi:7001"
    assert service.limit_key_for(two) == "suwayomi:7002"
    assert service.limit_key_for(one) in {s.limit_key for s in subs}

    engine: Engine = create_db_engine(tmp_path / "limits.db")
    metadata.create_all(engine)
    slots = DomainSlots(engine, ManualClock(datetime(2026, 11, 5, tzinfo=UTC)))
    for key, count in service.limit_slots(subs).items():
        slots.declare(key, count)
    cap = service.manifest.rate_limit.concurrency
    assert all(slots.try_acquire(service.limit_key_for(one)) for _ in range(cap))
    assert not slots.try_acquire(service.limit_key_for(one))  # source one is saturated...
    assert slots.try_acquire(service.limit_key_for(two))  # ...source two is not
    engine.dispose()


# --- EF-SRC-033: shape of the service ---------------------------------------------------------
@pytest.mark.req("EF-SRC-033")
def test_followed_series_are_the_library_not_every_known_manga() -> None:
    service, f, _ = make()
    followed = run(service.followed_series(f))
    assert {r.title for r in followed} == {"Series Alpha", "Series Beta"}
    assert all(r.source_key.startswith("suwayomi:") for r in followed)


@pytest.mark.req("EF-SRC-033")
def test_always_on_mode_request_refresh_sends_nothing() -> None:
    service, f, server = make()
    refs = list(series_by_title(service, f).values())
    before = len(server.requests)
    run(service.request_refresh(f, refs))
    assert len(server.requests) == before  # no refresh mutation, no request at all
    assert not any(
        {"fetchChapters", "updateLibrary", "fetchManga"} & r.fields for r in server.requests
    )


@pytest.mark.req("EF-SRC-033")
def test_server_queue_ticket_goes_queued_then_ready_and_cancel_dequeues() -> None:
    service, f, server = make()
    ref, units = alpha(service, f)
    ticket = run(service.request(f, UnitRef(series=ref, unit=units[0])))
    assert ticket.target_kind == "unit"
    seen = [run(service.poll(f, ticket)) for _ in range(3)]
    assert seen[0] in ("queued", "downloading") and seen[-1] == "ready"
    other = run(service.request(f, UnitRef(series=ref, unit=units[1])))
    assert other.token != ticket.token  # one ticket per target
    run(service.cancel(f, other))
    assert any("dequeueChapterDownload" in r.fields for r in server.requests)


# --- EF-SRC-036: upstream identity survives a reinstall ---------------------------------------
@pytest.mark.req("EF-SRC-036", "EF-SRC-008")
def test_suwayomi_reinstall_keeps_variant_identity() -> None:
    service, f, server = make()
    ref, before = alpha(service, f)
    ids_before = [u.remote_unit_id for u in before]
    handles_before = [u.memo["fetch_handle"] for u in before]

    server.reinstall()  # the tool is recreated: every local integer changes
    fresh = SuwayomiService(BASE_URL)
    ref_after, after = alpha(fresh, f)

    assert (ref_after.source_key, ref_after.remote_id) == (ref.source_key, ref.remote_id)
    assert [u.remote_unit_id for u in after] == ids_before  # no new Variant would appear
    assert [u.memo["fetch_handle"] for u in after] != handles_before
    # an old Unit still carries the dead handle: it is re-resolved by (sourceId, chapter.url)
    stale = before[0]
    payload = run(fresh.fetch_unit(f, ref, stale))
    assert isinstance(payload, FileAsset)
    new_id = server.local_chapter_ids()["/series-alpha/ch-001"]
    assert payload.request.url == f"{BASE_URL}/api/v1/chapter/{new_id}/download"
    assert run(f.download(payload.request)).sha256 == server.cbz_sha256("/series-alpha/ch-001")


@pytest.mark.req("EF-SRC-036")
def test_unit_dates_zero_upload_is_none_and_team_is_carried() -> None:
    service, f, _ = make()
    _, units = alpha(service, f)
    assert units[0].team == "Synthetic Team A"
    assert units[0].published_at == datetime.fromtimestamp(1760000000, UTC)
    assert units[1].published_at is None  # a zero upload date is never 1970-01-01
    assert [u.source_order for u in units] == sorted(u.source_order for u in units)


@pytest.mark.req("EF-SRC-008", "EF-SRC-033")
def test_error_codes_and_version_range_follow_catalog_and_manifest() -> None:
    import json as _json

    from shelfhold.events.operating import OPERATING_EVENTS
    from shelfhold.interfaces.http_api.catalog import all_error_codes
    from shelfhold.plugin_host.adapters.meta.suwayomi.versions import (
        MAX_EXCLUSIVE,
        MIN_VERSION,
        is_supported,
    )
    from shelfhold.plugin_host.errors import AuthExpired

    catalog = {c.code: c for c in all_error_codes()}
    for error in (ServiceVersionMismatch, AuthModeUnsupported, AuthExpired):
        assert error.code in catalog and catalog[error.code].event
        assert error.code in OPERATING_EVENTS  # same identifier as the event (EF-UX-003)

    manifest = Path(__file__).resolve().parents[3] / "sources" / "meta_suwayomi" / "manifest.json"
    declared = _json.loads(manifest.read_text("utf-8"))["supported_versions"]
    assert declared == {
        "min": ".".join(map(str, MIN_VERSION)),
        "max_exclusive": ".".join(map(str, MAX_EXCLUSIVE)),
    }
    assert is_supported("v2.4.2366") and is_supported("v2.9.0")
    assert not is_supported("v2.4.2365") and not is_supported("v3.0.0")
    assert not is_supported("nightly")
