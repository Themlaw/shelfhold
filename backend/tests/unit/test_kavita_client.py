"""EF-EXP-009 / EF-EXP-001 / ENF-COMP-001 / ENF-SEC-013: the Kavita client on a simulated server."""

from __future__ import annotations

import base64
import copy
import json
from datetime import timedelta

import pytest

from shelfhold.events.waiting_state import WaitingState
from shelfhold.export.application.target_health import TargetHealth
from shelfhold.export.domain.kavita_compat import (
    MINIMUM_VERSION,
    KavitaVersion,
    TargetMode,
    assess_version,
    minimum_version_for,
)
from shelfhold.export.targets.kavita import (
    HIDDEN_DIR_PATTERNS,
    AdminKey,
    ConfirmationRequired,
    KavitaClient,
    KavitaDestination,
    LibraryType,
    PlanNotVerified,
    ProtocolError,
    RouteMissing,
    TargetDegraded,
    TargetForbidden,
    public_config,
)
from shelfhold.sdk.export_target import ApiCall
from tests.fakes.kavita import API_KEY, BASE_URL, build_fetcher
from tests.kavita_env import NOW, OPENAPI_DIR, harness, run

EPUB = "Novel Z - Ch.003.epub"
CBZ = "Series A - Ch.012.cbz"


# -- ENF-COMP-001: one range, one minimum ------------------------------------------------------


@pytest.mark.req("ENF-COMP-001", "EF-EXP-009")
@pytest.mark.parametrize("ceiling", [None, 7, 13, 16, 18, 99])
def test_kavita_minimum_is_0_9_1_for_all_shelves(ceiling: int | None) -> None:
    assert minimum_version_for(ceiling) == MINIMUM_VERSION == KavitaVersion.parse("0.9.1")
    below = ["0.9.0.2", "0.9.0", "0.8.9.1", "0.8.9", "0.7.14.10"]
    inside = ["0.9.1", "0.9.1.0", "v0.9.1.4", "0.9.2", "0.9.9.9"]
    outside_above = ["0.10.0", "1.0.0"]
    for raw in below + outside_above + ["", "banana", "0.9"]:
        assert assess_version(raw).mode is TargetMode.DEGRADED_READONLY, raw
    for raw in inside:
        assert assess_version(raw).mode is TargetMode.FULL, raw


@pytest.mark.req("ENF-COMP-001", "EF-EXP-009")
def test_unsupported_kavita_version_degrades_to_plan_only() -> None:
    h = harness(version="0.9.0.2")
    assessment = run(h.client.probe())
    assert assessment.mode is TargetMode.DEGRADED_READONLY
    assert h.client.health.mode is TargetMode.DEGRADED_READONLY
    (detail,) = h.events.details("target.version_unsupported")
    assert detail["version"] == "0.9.0.2" and detail["minimum"] == "0.9.1"
    # the preview (reads) still works ...
    assert run(h.client.parse_name(CBZ, LibraryType.MANGA)).series_name == "Series A"
    assert len(run(h.client.list_libraries())) == 2
    # ... and nothing is ever written
    with pytest.raises(TargetDegraded):
        run(h.client.scan_folder("/manga/Series A"))
    with pytest.raises(TargetDegraded):
        run(h.client.create_library("Synthetic", LibraryType.MANGA, ["/manga"]))
    with pytest.raises(TargetDegraded):
        run(h.client.upload_series_cover(1, b"x"))
    assert h.fake.writes == []
    run(h.client.probe())  # the same version is announced once
    assert h.events.codes() == ["target.version_unsupported"]


# -- EF-EXP-009: 404 on a listed route => degraded_feature ------------------------------------


@pytest.mark.req("EF-EXP-009", "ENF-COMP-001")
@pytest.mark.parametrize(
    ("method", "path", "action"),
    [
        ("GET", "/api/Plugin/parse", lambda c: c.parse_name(CBZ, LibraryType.MANGA)),
        ("POST", "/api/Plugin/parse-bulk", lambda c: c.parse_bulk([CBZ], LibraryType.MANGA)),
    ],
)
def test_kavita_route_404_sets_degraded_mode_never_plans_without_collision_check(
    method: str, path: str, action: object
) -> None:
    h = harness()
    h.fake.remove_route(method, path)
    run(h.client.probe())
    assert h.client.health.mode is TargetMode.FULL and h.client.collision_check_available
    with pytest.raises(RouteMissing) as raised:
        run(action(h.client))  # type: ignore[operator]
    assert raised.value.route == f"{method} {path}"
    health = h.client.health
    assert health.mode is TargetMode.DEGRADED_FEATURE
    assert health.waiting_state is WaitingState.ROUTE_MISSING
    assert health.missing_routes == (f"{method} {path}",)
    assert h.events.details("target.route_missing") == [{"route": f"{method} {path}"}]
    # no plan is applied, the preview is marked "not verified"
    assert not h.client.collision_check_available and not h.client.preview_verified
    with pytest.raises(PlanNotVerified):
        h.client.require_verified_plan()
    # the route is not asked again, and the event is not repeated
    count = len(h.fake.requests)
    with pytest.raises(RouteMissing):
        run(action(h.client))  # type: ignore[operator]
    assert len(h.fake.requests) == count and len(h.events.details("target.route_missing")) == 1
    # recheck after Kavita is updated
    h.fake.missing.clear()
    run(h.client.recheck())
    assert h.client.health.mode is TargetMode.FULL and h.client.health.waiting_state is None
    h.client.require_verified_plan()


@pytest.mark.req("EF-EXP-009")
def test_kavita_route_404_elsewhere_cuts_only_that_feature() -> None:
    h = harness()
    h.fake.remove_route("POST", "/api/Upload/series")
    with pytest.raises(RouteMissing):
        run(h.client.upload_series_cover(1, b"x"))
    assert h.client.health.mode is TargetMode.DEGRADED_FEATURE
    h.client.require_verified_plan()  # the name check still works: plans may be applied
    assert run(h.client.parse_name(CBZ, LibraryType.MANGA)).series_name == "Series A"


# -- EF-EXP-009: library update, exclude patterns ---------------------------------------------


@pytest.mark.req("EF-EXP-009")
def test_kavita_library_update_get_then_faithful_put() -> None:
    h = harness()
    run(h.client.probe())
    start = len(h.fake.requests)
    before = copy.deepcopy(h.fake.libraries[0])
    update = run(h.client.ensure_exclude_patterns(7, HIDDEN_DIR_PATTERNS))
    assert update.changed and update.previous_exclude_patterns == ("*.tmp",)
    assert [(r.method, r.path) for r in h.fake.requests[start:]] == [
        ("GET", "/api/Library/libraries"),
        ("POST", "/api/Library/update"),
    ]  # read first, then write
    sent = h.fake.requests[-1].json()
    assert h.fake.violations == []  # every required field of UpdateLibraryDto, none unknown
    # the document sent is the one read, with the single change wanted
    assert sent["excludePatterns"] == ["*.tmp", *HIDDEN_DIR_PATTERNS]
    assert sent["fileGroupTypes"] == before["libraryFileTypes"]
    # the whole write DTO, derived from `before` independently of the code under test; the
    # only documented differences: `fileGroupTypes` is read as `libraryFileTypes`, and the
    # patterns gain the hidden-dir one
    document = json.loads((OPENAPI_DIR / "pinned.json").read_text("utf-8"))
    writable = set(document["components"]["schemas"]["UpdateLibraryDto"]["properties"])
    expected = {k: before[k] for k in writable if k in before}
    expected["fileGroupTypes"] = before["libraryFileTypes"]
    expected["excludePatterns"] = ["*.tmp", *HIDDEN_DIR_PATTERNS]
    assert sent == expected
    after = h.fake.libraries[0]
    assert {k: v for k, v in after.items() if k != "excludePatterns"} == {
        k: v for k, v in before.items() if k != "excludePatterns"
    }
    # idempotent: nothing to change, nothing written
    again = run(h.client.ensure_exclude_patterns(7, HIDDEN_DIR_PATTERNS))
    assert not again.changed
    assert len(h.fake.requests_to("/library/update")) == 1


@pytest.mark.req("EF-EXP-009")
def test_exclude_patterns_set_for_hidden_dirs() -> None:
    assert HIDDEN_DIR_PATTERNS and all(".shelfhold" in p for p in HIDDEN_DIR_PATTERNS)
    h = harness()
    created = run(h.client.create_library("Synthetic Novels", LibraryType.LIGHT_NOVEL, ["/novels"]))
    assert created["excludePatterns"] == list(HIDDEN_DIR_PATTERNS)
    assert created["type"] == int(LibraryType.LIGHT_NOVEL)
    assert h.fake.violations == []


@pytest.mark.req("EF-EXP-009")
def test_adopted_library_never_modified_without_confirmation() -> None:
    h = harness()
    with pytest.raises(ConfirmationRequired):
        run(h.client.ensure_exclude_patterns(7, HIDDEN_DIR_PATTERNS, adopted=True))
    assert h.fake.writes == [] and h.fake.libraries[0]["excludePatterns"] == ["*.tmp"]
    done = run(
        h.client.ensure_exclude_patterns(7, HIDDEN_DIR_PATTERNS, adopted=True, confirmed=True)
    )
    assert done.changed and done.previous_exclude_patterns == ("*.tmp",)  # kept for detach


# -- EF-EXP-009: scan-folder debounced --------------------------------------------------------


@pytest.mark.req("EF-EXP-009")
def test_scan_folder_debounced() -> None:
    h = harness()
    client, clock = h.client, h.clock
    client.request_scan("/manga/Series A/ch1")
    client.request_scan("/manga/Series A/ch1")  # duplicate
    clock.advance(seconds=5)
    client.request_scan("/manga/Series A")  # the parent covers the child
    client.request_scan("/manga/Series B")
    clock.advance(seconds=20)
    assert run(client.flush_scans()) == []  # still inside the quiet period
    assert h.fake.scans == []
    clock.advance(seconds=30)
    assert run(client.flush_scans()) == ["/manga/Series A", "/manga/Series B"]
    assert h.fake.scans == ["/manga/Series A", "/manga/Series B"] and h.fake.ignored_scans == []
    assert run(client.flush_scans()) == [] and len(h.fake.scans) == 2  # nothing left
    client.request_scan("/manga/Series A")  # a later request starts a new quiet period
    assert run(client.flush_scans()) == []
    clock.advance(minutes=1)
    assert run(client.flush_scans()) == ["/manga/Series A"]


# -- EF-EXP-009: plugin/parse, covers ---------------------------------------------------------


@pytest.mark.req("EF-EXP-009")
def test_plugin_parse_receives_library_type() -> None:
    h = harness(manga_parse_only=False)  # only the transport of the type is looked at here
    result = run(h.client.parse_name(EPUB, LibraryType.LIGHT_NOVEL))
    assert (result.series_name, result.min_chapter) == ("Novel Z", 3.0)
    run(h.client.parse_bulk([CBZ, EPUB], LibraryType.COMIC))
    assert h.fake.parse_calls == [
        {"name": EPUB, "libraryType": 4},
        {"name": CBZ, "libraryType": 1},
        {"name": EPUB, "libraryType": 1},
    ]
    call = KavitaDestination().parse_name_call(EPUB, "LightNovel")
    assert call is not None and call.query["libraryType"] == 4 and call.query["name"] == EPUB


@pytest.mark.req("EF-EXP-009")
def test_parse_bulk_splits_into_batches_of_100() -> None:
    h = harness()
    names = [f"Series A - Ch.{n:03d}.cbz" for n in range(250)]
    results = run(h.client.parse_bulk(names, LibraryType.MANGA))
    assert len(results) == 250 and results[names[249]].min_chapter == 249.0
    assert len(h.fake.requests_to("parse-bulk")) == 3 and h.fake.violations == []


@pytest.mark.req("EF-EXP-009")
def test_cover_upload_base64_without_data_prefix() -> None:
    h = harness()
    image = b"\x89PNG\r\n synthetic cover"
    run(h.client.upload_series_cover(42, image))
    body = h.fake.requests[-1].json()
    assert body["url"] == base64.b64encode(image).decode() and not body["url"].startswith("data:")
    assert body["id"] == 42 and body["lockCover"] is True
    assert h.fake.covers[42] == image and h.fake.violations == []


# -- ENF-SEC-013: the admin key stays in the core ---------------------------------------------


class _ThirdPartyPlugin:
    """A destination plugin: it describes calls, it never executes them and never sees a key."""

    def __init__(self) -> None:
        self.received: list[object] = []

    def calls(self, cfg: dict[str, object], names: list[str]) -> list[ApiCall]:
        self.received.append(cfg)
        destination = KavitaDestination()
        parse = [destination.parse_name_call(name, str(cfg["library_type"])) for name in names]
        out = [c for c in parse if c is not None]
        out.extend(destination.scan_calls(["/manga/Series A"]))
        self.received.append(out)
        return out


@pytest.mark.req("ENF-SEC-013", "EF-EXP-001")
def test_admin_key_never_passed_to_target_plugin() -> None:
    h = harness()
    stored = {"url": BASE_URL, "library_type": "Manga", "api_key": API_KEY, "token": API_KEY}
    cfg = public_config(stored)
    plugin = _ThirdPartyPlugin()
    calls = plugin.calls(cfg, [CBZ])
    run(h.client.probe())
    for call in calls:
        run(h.client.execute(call))  # the core injects the key, the plugin never holds it
    # nothing handed to the plugin carries the key, in any form
    for seen in plugin.received:
        dumped = json.dumps(seen, default=lambda o: o.model_dump(mode="json"))
        assert API_KEY not in dumped and API_KEY not in repr(seen)
    assert "api_key" not in cfg and "token" not in cfg and cfg["library_type"] == "Manga"
    # the key reaches Kavita in the header (and in the body of scan-folder, as the DTO wants)
    assert {r.headers["x-api-key"] for r in h.fake.requests} == {API_KEY}
    assert h.fake.scans == ["/manga/Series A"] and h.fake.violations == []
    assert API_KEY not in "".join(r.raw_url for r in h.fake.requests)
    # and it leaks from no object, event or error of the core
    h.fake.demote()
    with pytest.raises(TargetForbidden) as raised:
        run(h.client.scan_folder("/manga/Series B"))
    surfaces = [
        repr(h.client),
        str(h.client.health.to_json()),
        str(raised.value),
        repr(raised.value),
        json.dumps(h.events.emitted),
        repr(AdminKey(API_KEY)),
        str(AdminKey(API_KEY)),
    ]
    assert all(API_KEY not in s for s in surfaces)


@pytest.mark.req("ENF-SEC-013")
def test_kavita_403_pauses_destination_target_forbidden() -> None:
    h = harness()
    run(h.client.probe())
    h.fake.demote()  # the dedicated account is no longer an administrator
    with pytest.raises(TargetForbidden) as raised:
        run(h.client.scan_folder("/manga/Series A"))
    assert raised.value.route == "POST /api/Library/scan-folder"
    health = h.client.health
    assert health.waiting_state is WaitingState.TARGET_FORBIDDEN and health.paused
    assert h.events.details("target.forbidden") == [{"route": "POST /api/Library/scan-folder"}]
    # paused: nothing more is sent, on any route, and the event is not repeated
    count = len(h.fake.requests)
    with pytest.raises(TargetForbidden):
        run(h.client.list_libraries())
    with pytest.raises(TargetForbidden):
        run(h.client.ensure_exclude_patterns(7, HIDDEN_DIR_PATTERNS))
    assert len(h.fake.requests) == count and len(h.events.details("target.forbidden")) == 1
    # reconnect (target reconnect) after the account got its rights back
    h.fake.is_admin = True
    run(h.client.reconnect())
    assert not h.client.health.paused and h.client.health.waiting_state is None
    run(h.client.scan_folder("/manga/Series A"))
    assert h.fake.scans == ["/manga/Series A"]


@pytest.mark.req("ENF-SEC-013", "EF-EXP-009")
def test_kavita_403_on_another_route_pauses_too() -> None:
    h = harness()
    h.fake.demote()
    with pytest.raises(TargetForbidden) as raised:
        run(h.client.create_library("Synthetic", LibraryType.MANGA, ["/manga"]))
    assert raised.value.route == "POST /api/Library/create"
    assert h.client.health.paused


# -- EF-EXP-009: the named key expires --------------------------------------------------------


@pytest.mark.req("EF-EXP-009")
def test_key_expiring_event_j14_j1() -> None:
    h = harness()
    expires = NOW + timedelta(days=30)
    h.fake.key_expires_at = expires

    def check(days_before: float) -> list[str]:
        h.clock.set(expires - timedelta(days=days_before))
        return [n.threshold for n in run(h.client.check_key_expiry())]

    assert check(30) == [] and check(15) == []
    assert check(14) == ["J14"] and check(14) == [] and check(10) == [] and check(2) == []
    assert check(1) == ["J1"] and check(1) == [] and check(0.2) == []
    assert h.events.codes() == ["target.key_expiring", "target.key_expiring"]
    first, second = h.events.details("target.key_expiring")
    assert (first["threshold"], first["days_left"]) == ("J14", 14)
    assert (second["threshold"], second["days_left"]) == ("J1", 1)
    assert first["expires_at"] == expires.isoformat()
    assert h.client.health.waiting_state is WaitingState.KEY_EXPIRING
    assert not h.client.health.paused  # a key about to expire does not stop exports
    # the notices survive a restart: the health document is stored by the caller
    reborn = KavitaClient(
        build_fetcher(h.fake), BASE_URL, AdminKey(API_KEY), events=h.events, clock=h.clock,
        health=TargetHealth.from_json(h.client.health.to_json()),
    )  # fmt: skip
    assert run(reborn.check_key_expiry()) == []
    # a rotated key (new date) starts again
    rotated = NOW + timedelta(days=120)
    h.fake.key_expires_at = rotated
    h.clock.set(rotated - timedelta(days=40))
    assert run(h.client.check_key_expiry()) == []
    assert h.client.health.waiting_state is None
    h.clock.set(rotated - timedelta(days=14))
    assert [n.threshold for n in run(h.client.check_key_expiry())] == ["J14"]


@pytest.mark.req("EF-EXP-009")
def test_key_expiry_jumping_straight_to_j1_sends_one_notice() -> None:
    h = harness()
    h.fake.key_expires_at = NOW + timedelta(hours=20)
    assert [n.threshold for n in run(h.client.check_key_expiry())] == ["J1"]
    assert run(h.client.check_key_expiry()) == []
    assert len(h.events.details("target.key_expiring")) == 1


@pytest.mark.req("EF-EXP-009")
def test_key_without_expiry_is_reported_not_notified() -> None:
    h = harness()
    h.fake.key_expires_at = None
    assert run(h.client.check_key_expiry()) == []
    assert h.client.health.key_expires_at is None and h.events.emitted == []


@pytest.mark.req("ENF-COMP-001")
def test_unreadable_version_is_degraded_readonly_and_blocks_plans() -> None:
    assert assess_version(None).mode is TargetMode.DEGRADED_READONLY
    h = harness()
    h.fake.remove_route("GET", "/api/Server/server-info-slim")
    with pytest.raises(RouteMissing):
        run(h.client.probe())
    assert h.client.health.version is None
    with pytest.raises(TargetDegraded):
        h.client.require_verified_plan()
    with pytest.raises(RouteMissing):  # and no write goes out meanwhile
        run(h.client.scan_folder("/manga/Series A"))
    assert h.fake.writes == []


@pytest.mark.req("EF-EXP-009")
def test_kavita_404_with_json_body_is_a_missing_resource_not_a_missing_route() -> None:
    h = harness()
    run(h.client.probe())
    h.fake.not_found_resource("GET", "/api/Library/type")
    with pytest.raises(ProtocolError):
        run(h.client.library_type(7))
    assert h.client.health.mode is TargetMode.FULL and h.client.health.missing_routes == ()
    assert h.events.details("target.route_missing") == []


@pytest.mark.req("ENF-SEC-013")
def test_scan_folder_always_carries_the_key_and_refuses_a_non_object_body() -> None:
    h = harness()
    run(h.client.probe())
    run(
        h.client.execute(
            ApiCall(method="POST", path="/api/Library/scan-folder", auth="target_admin")
        )
    )
    assert h.fake.ignored_scans == [] and len(h.fake.scans) == 1  # the key was in the body
    before = len(h.fake.requests)
    bad = ApiCall(
        method="POST", path="/api/Library/scan-folder", json_body=["x"], auth="target_admin"
    )
    with pytest.raises(ProtocolError):
        run(h.client.execute(bad))
    assert len(h.fake.requests) == before


@pytest.mark.req("ENF-SEC-013")
def test_public_config_filters_recursively_by_whole_word() -> None:
    cfg = public_config({
        "author": "A", "library_type": "Manga", "apiKey": "x", "X-Api-Key": "x",
        "nested": {"token": "x", "author_name": "B", "deep": [{"password": "x", "ok": 1}]},
    })  # fmt: skip
    assert cfg == {
        "author": "A", "library_type": "Manga",
        "nested": {"author_name": "B", "deep": [{"ok": 1}]},
    }  # fmt: skip


@pytest.mark.req("EF-EXP-009")
def test_ids_are_coerced_to_int_at_entry() -> None:
    h = harness()
    assert run(h.client.library_type("7")) is LibraryType.MANGA  # type: ignore[arg-type]
    assert run(h.client.ensure_exclude_patterns("7", HIDDEN_DIR_PATTERNS)).changed  # type: ignore[arg-type]
    run(h.client.upload_series_cover("42", b"x"))  # type: ignore[arg-type]
    assert h.fake.requests_to("/upload/series")[0].json()["id"] == 42
    with pytest.raises(ValueError):
        run(h.client.library_type("7; DROP"))  # type: ignore[arg-type]


@pytest.mark.req("ENF-OPS-011", "ENF-SEC-013")
def test_kavita_401_revoked_key_pauses_destination_with_target_unreachable() -> None:
    """Matrix row `kavita_key_revoked`: a 401 is an immediate `target.unreachable`, the destination
    is disabled (nothing more is sent) until `target reconnect`."""
    h = harness()
    h.client._key = AdminKey("the-key-was-revoked-0123456789")  # type: ignore[attr-defined]
    with pytest.raises(TargetForbidden) as raised:
        run(h.client.list_libraries())
    assert raised.value.code == "target.unreachable"  # the code of the event, not target.forbidden
    assert h.events.codes() == ["target.unreachable"]
    assert h.client.health.paused
    count = len(h.fake.requests)
    with pytest.raises(TargetForbidden) as again:
        run(h.client.list_libraries())
    assert again.value.code == "target.unreachable"
    assert len(h.fake.requests) == count and h.events.codes() == ["target.unreachable"]
