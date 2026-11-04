"""EF-EXP-009 / ENF-COMP-001: the allow-list of Kavita routes against the frozen openapi."""

from __future__ import annotations

import json

import pytest

from shelfhold.export.targets.kavita import ALLOWED_ROUTES, LibraryType, RouteNotAllowed
from shelfhold.sdk.export_target import ApiCall
from tests.fakes.kavita import API_KEY
from tests.kavita_env import OPENAPI_DIR, documented_routes, harness, run

FIXTURES = ("0.9.1", "pinned", "latest")


def _openapi(name: str) -> dict[str, object]:
    document: dict[str, object] = json.loads((OPENAPI_DIR / f"{name}.json").read_text("utf-8"))
    return document


@pytest.mark.req("EF-EXP-009", "ENF-COMP-001")
@pytest.mark.parametrize("fixture", FIXTURES)
def test_kavita_allowlisted_routes_exist_in_pinned_openapi(fixture: str) -> None:
    documented = documented_routes()
    assert documented, "docs/kavita-routes.md lists no route"
    assert documented == {(r.method, r.path) for r in ALLOWED_ROUTES}  # doc, code: one list
    paths = _openapi(fixture)["paths"]
    assert isinstance(paths, dict)
    for method, route in sorted(documented):
        assert route in paths, f"{route} is not in the openapi of {fixture}"
        assert method.lower() in paths[route], f"{method} {route} is not in {fixture}"
    # the fixture is reduced to the allow-list: nothing else sneaks in
    assert set(paths) == {route for _, route in documented}


@pytest.mark.req("EF-EXP-009")
def test_kavita_library_type_matches_the_openapi_enum() -> None:
    components = _openapi("pinned")["components"]
    assert isinstance(components, dict)
    schema = components["schemas"]["LibraryType"]
    assert {t.kavita_name: int(t) for t in LibraryType} == dict(
        zip(schema["x-enum-varnames"], schema["enum"], strict=True)
    )


@pytest.mark.req("EF-EXP-009", "ENF-SEC-013")
def test_kavita_adapter_calls_only_allowlisted_routes() -> None:
    h = harness()
    client = h.client

    async def session() -> None:
        await client.probe()
        await client.check_key_expiry()
        await client.parse_name("Series A - Ch.001.cbz", LibraryType.MANGA)
        await client.parse_bulk(["Series A - Ch.001.cbz"], LibraryType.MANGA)
        await client.list_libraries()
        await client.library_type(7)
        await client.create_library("Synthetic Novels", LibraryType.LIGHT_NOVEL, ["/novels"])
        await client.ensure_exclude_patterns(8, ["**/.shelfhold/**"])
        await client.scan_folder("/manga/Series A")
        await client.upload_series_cover(42, b"\x89PNG synthetic")

    run(session())
    seen = {(r.method, r.path) for r in h.fake.requests}
    assert h.fake.violations == []  # the referee's own list agrees
    assert seen == documented_routes()  # every route is used, none else is
    assert API_KEY not in "".join(r.raw_url for r in h.fake.requests)  # never in a URL


@pytest.mark.req("EF-EXP-009")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/Library/delete"),
        ("DELETE", "/api/Library/delete"),
        ("GET", "/api/Plugin/version"),  # takes the key as a query parameter: never used
        ("POST", "/api/Account/rotate-auth-key"),
        ("POST", "/api/Library/scan-folder/../delete"),
        ("POST", "/api/Library/scan-folder?x=1"),
        ("DELETE", "/api/Library/libraries"),
        ("GET", "http://elsewhere.example/api/Library/libraries"),
    ],
)
def test_kavita_off_list_call_is_refused_before_any_request(method: str, path: str) -> None:
    h = harness()
    with pytest.raises(RouteNotAllowed):
        run(h.client.execute(ApiCall(method=method, path=path, auth="target_admin")))
    assert h.fake.requests == []
