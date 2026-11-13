"""M3 L09 (EF-USR-006, EF-USR-016, ENF-SEC-010): a child sees nothing that is not rated, nothing
above her ceiling, on any surface, and a Work she may not see is exactly like a Work that does not
exist.

Everything goes through the real application (`create_app()`, real sessions, real CSRF). The
single rule is a `ViewerScope` applied to the query: nothing is filtered afterwards, so a page, a
count or a cursor cannot carry a hidden row out.

The meta-test holds the list of routes: a route of the OpenAPI document with no child case here
fails the suite. A route that a child can use (OPEN) names the test that attacks it.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.routing import APIWebSocketRoute, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute

from shelfhold.catalog.public import (
    count_visible_works,
    list_visible_works,
    visible_edition_ids,
    visible_work_ids,
    work_is_visible,
)
from shelfhold.config import Settings
from shelfhold.events.activity import ActivityEvent, Viewer
from shelfhold.events.projection import project_event
from shelfhold.identity.public import local_auth, reader_scope, scope_of
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, run_cli
from tests.visibility_env import HIDDEN_TITLES, VISIBLE_TITLES, Seed, add_profile, seed_catalogue

ULID = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
MISSING = "01HZZZZZZZZZZZZZZZZZZZZZZM"
SEARCH = "/api/v1/search"
ME = "/api/v1/me"

# -- the child case of every route of the OpenAPI document -------------------------------------
# PUBLIC: needs no session and serves no catalogue data. OWN: the caller's own account, never
# catalogue data. OPEN: a child may use it and sees only what the filter lets through; the value
# is the surface that `test_unrated_invisible_to_child_on_all_surfaces` attacks. CLOSED: refused
# to a child (403), by session and by key.

PUBLIC = (
    "GET /api/v1/source",
    "GET /robots.txt",
    "GET /login",
    "GET /login/passkey.js",
    "POST /api/v1/auth/login",
    "POST /api/v1/auth/passkeys/login",
    "POST /api/v1/auth/passkeys/login/options",
    "GET /healthz",
    "GET /readyz",
)
OWN = (
    "GET /api/v1/me",
    "GET /api/v1/about",
    "POST /api/v1/auth/logout",
    "GET /api/v1/auth/passkeys",
    "POST /api/v1/auth/passkeys",
    "POST /api/v1/auth/passkeys/options",
    "POST /api/v1/auth/passkeys/reauth/options",
    "DELETE /api/v1/auth/passkeys/{passkey_id}",
    "GET /api/v1/auth/sessions",  # M3 L07: her own sessions, a label and dates, no catalogue data
    "POST /api/v1/auth/sessions/revoke-others",
    "DELETE /api/v1/auth/sessions/{session_id}",
    "POST /api/v1/auth/reauth",
    "GET /api/v1/auth/session",
    "GET /api/v1/me/preferences",
    "PATCH /api/v1/me/preferences",
    "GET /api/v1/me/title-languages",
    "PUT /api/v1/me/title-languages",
    # M3 L14: how she wants to be told, her own row only (no identifier names another profile)
    "GET /api/v1/me/notifications",
    "PUT /api/v1/me/notifications",
    "GET /api/v1/me/visibility",
    "PUT /api/v1/me/visibility",
    # M3 L35: her own requests, titled by what she was shown; a child asks by her reference only
    # (no Work identifier, so no title of the catalogue can come back), and another profile's
    # request is exactly an absent one (tests/integration/test_requests_http.py)
    "GET /api/v1/requests",
    "POST /api/v1/requests",
    "GET /api/v1/requests/{request_id}",
    "POST /api/v1/requests/{request_id}/cancel",
)
OPEN = {
    "GET /api/v1/activity": "activity",
    "GET /api/v1/events": "sse",
    "GET /api/v1/me/state": "me_state_list",
    "GET /api/v1/me/state/{work_id}": "me_state_get",
    "PUT /api/v1/me/state/{work_id}": "me_state_put",
    "GET /api/v1/me/works/{work_id}/title": "me_title",
    "GET /api/v1/search": "search",
    "GET /api/v1/works": "works_since",  # M3 L11: the list of Works and its `?since=` removals
}
CLOSED = (
    "GET /api/v1/openapi.json",
    "GET /api/v1/maintenance",  # M3 L13: the mode, the window and the queue are the admin's
    "POST /api/v1/maintenance/enter",
    "POST /api/v1/maintenance/exit",
    "POST /api/v1/queue/pause",
    "POST /api/v1/queue/resume",
    "POST /api/v1/jobs/{job_id}/cancel",
    "POST /api/v1/jobs/{job_id}/priority",
    "POST /api/v1/jobs/{job_id}/replay",
    "POST /api/v1/acquire",
    "POST /api/v1/activity/{ref}/unmask",
    "POST /api/v1/adoption/{destination_id}/confirm-migration",
    "POST /api/v1/adoption/{destination_id}/follow-from",
    "GET /api/v1/adoption/{destination_id}/migration",
    "POST /api/v1/adoption/{destination_id}/pair",
    "POST /api/v1/adoption/{destination_id}/pair-auto",
    "GET /api/v1/adoption/{destination_id}/pairing",
    "POST /api/v1/adoption/{destination_id}/repatriate",
    "GET /api/v1/config",
    "PATCH /api/v1/config",
    "POST /api/v1/coverage/resplit/apply",
    "POST /api/v1/coverage/resplit/propose",
    "POST /api/v1/deletions",
    "POST /api/v1/deletions/preview",
    "POST /api/v1/detach",
    "GET /api/v1/editions",
    "POST /api/v1/editions/{edition_id}/add-directly",
    "GET /api/v1/editions/{edition_id}/backfill",
    "POST /api/v1/editions/{edition_id}/backfill",
    "POST /api/v1/editions/{edition_id}/check",
    "POST /api/v1/editions/{edition_id}/external-ids/renumber",
    "POST /api/v1/editions/{edition_id}/fields/{field}/lock",
    "POST /api/v1/editions/{edition_id}/fields/{field}/unlock",
    "POST /api/v1/editions/{edition_id}/language",
    "POST /api/v1/editions/{edition_id}/series-name",
    "POST /api/v1/editions/{edition_id}/shelf",
    "GET /api/v1/editions/{edition_id}/shelf-proposal",
    "GET /api/v1/export-items/{item_id}",
    "DELETE /api/v1/follows",
    "POST /api/v1/follows",
    "GET /api/v1/health/page",
    "GET /api/v1/intents",
    "POST /api/v1/intents/{intent_id}/resolve",
    "GET /api/v1/jobs",
    "GET /api/v1/jobs/{job_id}",
    "POST /api/v1/jobs/{job_id}/retry",
    "GET /api/v1/merge-suggestions/works",
    "POST /api/v1/merge-suggestions/works/scan",
    "POST /api/v1/missing/{destination_id}/{export_item_id}/resolve",
    "POST /api/v1/plans",
    "POST /api/v1/plans/naming-preview",
    "POST /api/v1/plans/{plan_id}/apply",
    "GET /api/v1/plugins/{key}/attestations",
    "POST /api/v1/plugins/{key}/attestations",
    "DELETE /api/v1/plugins/{key}/attestations/{name}",
    "POST /api/v1/probe",
    "GET /api/v1/profiles",
    "POST /api/v1/profiles",
    "DELETE /api/v1/profiles/{profile_id}",
    "GET /api/v1/profiles/{profile_id}/export",
    "PATCH /api/v1/profiles/{profile_id}",
    "GET /api/v1/profiles/{profile_id}/source-policies",
    "PUT /api/v1/profiles/{profile_id}/source-policies/{source_key}",
    "PUT /api/v1/profiles/{profile_id}/family-search",
    "PUT /api/v1/profiles/{profile_id}/visibility",
    "POST /api/v1/reconcile/adopt-after-restore",
    "POST /api/v1/reconcile/forget-root",
    "POST /api/v1/restore/acknowledge",
    "GET /api/v1/revisions",
    "POST /api/v1/revisions/apply",
    "POST /api/v1/revisions/force",
    "POST /api/v1/roots/{root}/reinit",
    "POST /api/v1/roots/{root}/restored",
    "POST /api/v1/roots/{root}/takeover",
    "POST /api/v1/secrets/import",
    "POST /api/v1/series/{export_series_id}/resume",
    "GET /api/v1/setup/state",  # M3 L39: the first-run wizard is the admin's, on a fresh session
    "POST /api/v1/setup/adoption/confirm",
    "POST /api/v1/setup/keyring-export",  # M3 L40: the keyring leaves only as an age file
    "PUT /api/v1/setup/sources/family",  # M3 L40 (D13): which sources a child's search reaches
    "POST /api/v1/setup/adoption/preview",
    "POST /api/v1/setup/library",
    "POST /api/v1/setup/probe",
    "POST /api/v1/shelves",
    "POST /api/v1/shelves/precreate",
    "GET /api/v1/shelves",  # M3 L11: the list of shelves and its `?since=` removals
    "GET /api/v1/shelves/withdrawals",
    "POST /api/v1/shelves/withdrawals",
    "POST /api/v1/shelves/{shelf_id}/allowed-profiles",
    "POST /api/v1/shelves/{shelf_id}/destinations",
    "POST /api/v1/shelves/{shelf_id}/max-age",
    "POST /api/v1/shelves/{shelf_id}/skip-read-upgrades",
    "POST /api/v1/sources",
    "POST /api/v1/sources/suwayomi/recheck",
    "POST /api/v1/sources/suwayomi/reconnect",
    "POST /api/v1/sources/suwayomi/secret",
    "PUT /api/v1/sources/{key}/cookies",
    "DELETE /api/v1/sources/{key}/secrets/{name}",
    "PUT /api/v1/sources/{key}/secrets/{name}",
    "GET /api/v1/sources/{key}/settings",
    "PATCH /api/v1/sources/{key}/settings",
    "GET /api/v1/sources/{source_key}/filters",
    "GET /api/v1/sources/{source_key}/search",
    "GET /api/v1/stats",
    "GET /api/v1/status",
    "POST /api/v1/store/reinit",
    "POST /api/v1/targets",
    "POST /api/v1/targets/{target_id}/recheck",
    "POST /api/v1/targets/{target_id}/reconnect",
    "POST /api/v1/targets/{target_id}/rotate-key",
    "GET /api/v1/to-rate",
    "GET /api/v1/to-shelve",
    "POST /api/v1/tombstones/accept-lost-keyring",
    "POST /api/v1/tombstones/flush",
    "POST /api/v1/tombstones/set-aside-journal",
    "POST /api/v1/units/{unit_id}/purge",
    "POST /api/v1/units/{unit_id}/restore",
    "POST /api/v1/works/rating-batch",
    "POST /api/v1/works/{work_id}/purge",
    "POST /api/v1/works/{work_id}/rating",
    "GET /api/v1/requests/queue",  # M3 L35: what waits for an admin
    "GET /api/v1/requests/{request_id}/approval",
    "POST /api/v1/requests/{request_id}/approve",
    "POST /api/v1/requests/{request_id}/reject",
    "POST /api/v1/requests/{request_id}/not-found",
    "GET /api/v1/admin/sessions",  # M3 L07: every profile's sessions are the admin's
    "DELETE /api/v1/admin/sessions/{session_id}",
    "GET /api/v1/admin/recovery-kit",
)

# The one route whose body is read before its guard answers: it is given a valid one, so that the
# refusal under test is the child's, not a validation error.
BODIES = {"POST /api/v1/reconcile/forget-root": {"root_id": "x", "confirm": "x"}}

# Surfaces that do not exist yet: the lot that builds each one adds its child case, and this
# list is where the suite notices that the surface has arrived (the meta-test fails when a path
# with that word shows up in the OpenAPI document).
PENDING_SURFACES = {
    "thumbnails": ("L24", ("thumb", "thumbs", "thumbnail", "thumbnails")),
    "covers": ("L24", ("cover", "covers")),
    "opds": ("M5", ("opds",)),
}

SURFACES = (
    "search", "search_pages", "search_key", "federated", "me_state_get", "me_state_put",
    "me_state_list", "me_title", "activity", "notification", "works_since", "sse",
)  # fmt: skip
METHODS = ("get", "post", "put", "patch", "delete")
HIDDEN_FROM_CEILING_10 = {*HIDDEN_TITLES, "twelve"}
VISIBLE_FOR_CEILING_10 = {"ok", "adult_shelf", "mixed"}


class World:
    """The catalogue, a child with a ceiling of 10, an adult member and the admin."""

    def __init__(self, env: AuthEnv, seed: Seed, kid: TestClient, kid_id: str) -> None:
        self.env, self.seed, self.kid, self.kid_id = env, seed, kid, kid_id

    @property
    def markers(self) -> list[str]:
        """What must never reach a child: the titles and the ids of every hidden Work."""
        hidden = [self.seed.title_of(k) for k in HIDDEN_FROM_CEILING_10]
        return hidden + [self.seed.works[k] for k in HIDDEN_FROM_CEILING_10] + [
            self.seed.editions[k] for k in ("above", "adult_edition", "misfiled", "mixed_new")
        ]  # fmt: skip

    def clean(self, text: str) -> None:
        leaked = [m for m in self.markers if m in text]
        assert not leaked, f"a child's answer holds {leaked}"

    def put(self, path: str, body: Any) -> Any:
        return self.kid.put(path, json=body, headers={CSRF_HEADER: self.env.csrf(self.kid)})


FAMILY_SOURCE = "famsrc"
# What the family source answers: the titles of Works the admin rated (above her ceiling or not)
# and one title the catalogue knows nothing of.
REMOTE_RATED = ("above", "twelve", "adult_edition")
REMOTE_PLAIN = "Plain Remote Tale"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    import shelfhold.main as main_module
    from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
    from tests.federated_env import FakeSource, hit

    titles = [{**VISIBLE_TITLES, **HIDDEN_TITLES}[k] for k in REMOTE_RATED]
    source = FakeSource(
        FAMILY_SOURCE, name="Family source",
        pages=[[*(hit(f"r-{k}", t) for k, t in zip(REMOTE_RATED, titles, strict=True)),
                hit("r-plain", REMOTE_PLAIN)]],
    )  # fmt: skip
    registry = PluginRegistry(active_keys=None)
    registry.load_all(
        [PluginCandidate(key=source.key, origin=PluginOrigin.FIRST_PARTY, load=lambda: source)]  # type: ignore[arg-type]
    )
    monkeypatch.setattr(main_module, "start_plugins", lambda *_a, **_k: (registry, []))
    with auth_env(monkeypatch, settings) as env:
        yield env


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, api: AuthEnv) -> Iterator[World]:
    seed = seed_catalogue(api)
    kid = add_profile(api, "kid", "child", "10.0.0.2", max_age_rating=10)
    assert run_cli(monkeypatch, api.settings, "search", "rebuild") == 0
    assert api.login().status_code == 200
    kid_id = str(
        seed.engine.connect().execute(sa.text("SELECT id FROM user WHERE username='kid'")).scalar()
    )
    yield World(api, seed, kid, kid_id)
    seed.engine.dispose()


def _routes(app: Any) -> set[str]:
    """Every route of the application, the ones left out of the OpenAPI document included (a
    stream, a thumbnail or a cover is usually `include_in_schema=False`). A route with no HTTP
    method (a mounted application, a WebSocket) is counted too, as `MOUNT` or `WEBSOCKET`: it
    serves a child as well, and needs its case."""
    found: set[str] = set()
    for context in iter_route_contexts(app.routes):  # routers included lazily are walked too
        if context.path is None:
            continue
        own = context.methods or getattr(context.original_route, "methods", None) or set()
        methods = set(own) - {"HEAD", "OPTIONS"}
        if own:  # a HEAD-only route has only the twin of a GET: nothing more to serve
            found.update(f"{m} {context.path}" for m in methods)
        elif isinstance(context.original_route, WebSocketRoute | APIWebSocketRoute):
            found.add(f"WEBSOCKET {context.path}")
        else:
            found.add(f"MOUNT {context.path}")
    return found


def _shape(response: Any) -> tuple[Any, ...]:
    body = response.json()
    return response.status_code, body.get("type"), body.get("title"), body.get("detail")


def _titles(response: Any) -> list[str]:
    return [hit["title"] for hit in response.json()["items"]]


# -- the meta-test -----------------------------------------------------------------------------


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_every_openapi_route_has_a_child_case(api: AuthEnv) -> None:
    """A route added with no child case fails the suite (the end condition of L09)."""
    declared = [*PUBLIC, *OWN, *OPEN, *CLOSED]
    assert len(declared) == len(set(declared)), "a route holds two child cases"
    routes = _routes(api.app)
    assert not routes - set(declared), (
        f"routes with no child case: {sorted(routes - set(declared))}"
    )
    assert not set(declared) - routes, (
        f"cases of routes that are gone: {sorted(set(declared) - routes)}"
    )
    module = sys.modules[__name__]
    for surface in set(OPEN.values()):
        assert surface in SURFACES, surface
    assert hasattr(module, "test_unrated_invisible_to_child_on_all_surfaces")


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_pending_surfaces_get_a_real_child_case_when_they_arrive(api: AuthEnv) -> None:
    """SSE (L12), thumbnails (L24), notifications (L14), OPDS (M5): the day a path of that
    surface shows up in the OpenAPI document, this fails until its child case is written."""
    segments = {
        seg for route in _routes(api.app) for seg in route.split(" ", 1)[1].lower().split("/")
    }
    arrived = {
        name: lot for name, (lot, words) in PENDING_SURFACES.items() if segments & set(words)
    }
    assert not arrived, f"surfaces that arrived without a child case: {arrived}"


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_new_route_without_a_child_case_is_caught(api: AuthEnv) -> None:
    """The meta-test is not decoration: a route that no registry names is reported as missing."""
    known = set([*PUBLIC, *OWN, *OPEN, *CLOSED])
    assert _routes(api.app) - known == set()  # today: every route has its case
    assert ({*_routes(api.app), "GET /api/v1/zz-new-surface"} - known) == {
        "GET /api/v1/zz-new-surface"
    }


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_meta_test_counts_mounts_and_websockets() -> None:
    """A route with no HTTP method serves a child as well: it is listed, not skipped."""
    from fastapi import FastAPI
    from starlette.responses import PlainTextResponse

    app = FastAPI()
    app.mount(
        "/zz-static", lambda scope, receive, send: PlainTextResponse("x")(scope, receive, send)
    )
    app.add_api_websocket_route("/zz-socket", lambda websocket: None)
    assert _routes(app) >= {"MOUNT /zz-static", "WEBSOCKET /zz-socket"}


# -- the closed routes -----------------------------------------------------------------------


def _fill(route: str) -> tuple[str, str]:
    method, path = route.split(" ", 1)
    out = path
    while "{" in out:
        start, end = out.index("{"), out.index("}")
        out = out[:start] + ULID + out[end + 1 :]
    return method, out


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_child_is_refused_on_every_closed_route(world: World) -> None:
    """Session and key of a child: 403 on every route that is not hers, whatever the verb."""
    env, failures = world.env, []
    key = (
        local_auth(world.seed.engine, env.settings)
        .create_api_key(name="kidkey", username="kid", read_only=True)
        .value
    )
    for route in CLOSED:
        method, path = _fill(route)
        body: dict[str, Any] = {"json": BODIES.get(route, {})} if method != "GET" else {}
        by_session = world.kid.request(
            method, path, headers={CSRF_HEADER: env.csrf(world.kid)}, **body
        )
        by_key = env.other_client("10.0.0.50").request(
            method, path, headers={"Authorization": f"Bearer {key}"}, **body
        )
        for how, response in (("session", by_session), ("key", by_key)):
            if response.status_code != 403:
                failures.append((route, how, response.status_code))
            if response.status_code < 400:  # never a success, never data
                failures.append((route, how, "success"))
            world.clean(response.text)
    assert not failures, failures


MAINTENANCE_ROUTES = CLOSED[1:9]  # the eight routes of M3 L13, listed first in CLOSED


@pytest.mark.req("EF-USR-006", "EF-INT-018", "EF-INT-017")
def test_child_session_and_key_change_nothing_through_the_maintenance_and_queue_routes(
    world: World,
) -> None:
    """M3 L13: the mode, the window and the control of the queue are the admin's. A child, by her
    session or by her key, gets 403 on each route and nothing moves: the mode stays off, no pause
    is recorded, no job changes and no audit line is written."""
    assert all(
        "/maintenance" in r or "/queue/" in r or r.endswith(("cancel", "priority", "replay"))
        for r in MAINTENANCE_ROUTES
    )
    env, engine = world.env, world.seed.engine
    key = (
        local_auth(engine, env.settings)
        .create_api_key(name="kidkey2", username="kid", read_only=True)
        .value
    )

    def state() -> tuple[object, ...]:
        with engine.connect() as conn:
            return (
                conn.execute(sa.text("SELECT maintenance_since FROM instance")).scalar(),
                conn.execute(
                    sa.text("SELECT count(*) FROM app_setting WHERE key LIKE 'queue.%'")
                ).scalar(),
                conn.execute(sa.text("SELECT count(*) FROM job_view")).scalar(),
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM audit WHERE action LIKE 'maintenance.%' "
                        "OR action LIKE 'queue.%'"
                    )
                ).scalar(),
            )

    before = state()
    for route in MAINTENANCE_ROUTES:
        method, path = _fill(route)
        body: dict[str, Any] = {"json": {"source": "source-a", "priority_class": "new"}}
        by_session = world.kid.request(
            method,
            path,
            headers={CSRF_HEADER: env.csrf(world.kid)},
            **(body if method != "GET" else {}),
        )
        by_key = env.other_client("10.0.0.52").request(
            method,
            path,
            headers={"Authorization": f"Bearer {key}"},
            **(body if method != "GET" else {}),
        )
        assert by_session.status_code == 403, (route, "session", by_session.status_code)
        assert by_key.status_code == 403, (route, "key", by_key.status_code)
        world.clean(by_session.text + by_key.text)
    assert state() == before


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_own_and_public_routes_carry_no_catalogue_data(world: World) -> None:
    for route in (*OWN, *PUBLIC):
        method, path = _fill(route)
        if method != "GET":
            continue
        for client in (world.kid, world.env.other_client("10.0.0.51")):
            world.clean(client.get(path).text)


# -- one test, every surface -----------------------------------------------------------------


def _surface_search(world: World) -> None:
    response = world.kid.get(SEARCH, params={"q": "Tale", "limit": 200})
    assert response.status_code == 200, response.text
    assert sorted(_titles(response)) == sorted(
        world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10
    )
    world.clean(response.text)
    assert response.json()["total"] == len(VISIBLE_FOR_CEILING_10)  # a total never counts hidden
    nothing = world.kid.get(SEARCH, params={"q": "Zzzzqx"}).json()
    for key in HIDDEN_FROM_CEILING_10:  # a hidden Work is exactly like one that does not exist
        answer = world.kid.get(SEARCH, params={"q": world.seed.title_of(key)})
        assert answer.status_code == 200 and answer.json() == nothing, key
        assert world.kid.get(SEARCH, params={"q": world.seed.works[key]}).json() == nothing
    admin = world.env.client.get(SEARCH, params={"q": "Tale", "limit": 200}).json()
    assert len(admin["items"]) == len(VISIBLE_TITLES) + len(HIDDEN_TITLES)  # the barrier is hers


def _surface_search_pages(world: World) -> None:
    seen: list[str] = []
    for offset in range(0, 20):
        page = world.kid.get(SEARCH, params={"q": "Tale", "limit": 1, "offset": offset}).json()
        assert len(page["items"]) <= 1
        assert page["total"] == len(VISIBLE_FOR_CEILING_10)  # the same total on every page
        seen += [h["title"] for h in page["items"]]
        world.clean(str(page))
    assert sorted(seen) == sorted(world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10)
    for limit, offset in ((200, 0), (200, 2), (200, 3), (1, 10**6), (1, 3)):
        page = world.kid.get(SEARCH, params={"q": "Tale", "limit": limit, "offset": offset})
        world.clean(page.text)
    assert (
        world.kid.get(SEARCH, params={"q": "Tale", "limit": 200, "offset": 3}).json()["items"] == []
    )


def _surface_search_key(world: World) -> None:
    """A key of the child's profile reads as the child, never as whoever created it."""
    key = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="kidkey", username="kid", read_only=True)
        .value
    )
    answer = world.env.other_client("10.0.0.52").get(
        SEARCH, params={"q": "Tale", "limit": 200}, headers={"Authorization": f"Bearer {key}"}
    )
    assert answer.status_code == 200, answer.text
    assert sorted(_titles(answer)) == sorted(world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10)
    world.clean(answer.text)


def _surface_federated(world: World) -> None:
    """A remote result has no rating: the child's door is the family search (L19), a reduced form
    that holds no source that is not marked "family", and by key none at all. A family source that
    names Works the admin rated is the attack: they follow the catalogue (hidden above her
    ceiling), they are never offered as unrated results."""
    import sqlite3

    from tests.integration.test_family_search_review_poc import bind

    database = str(world.env.settings.database_path)
    ask = {"q": "Tale", "scope": "all"}
    answer = world.kid.get(SEARCH, params=ask)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert set(body) == {"items", "local", "state", "next_cursor"}  # no total, no count
    assert body["items"] == [] and body["state"] == "empty"  # hide mode, the default: no source
    assert sorted(h["title"] for h in body["local"]) == sorted(
        world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10
    )
    world.clean(answer.text)
    # the family source: ticked, the profile shows the reduced form, the catalogue knows three
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO source_config (plugin_id, family_safe) VALUES (?, 1)", (FAMILY_SOURCE,)
        )
        connection.execute(
            'UPDATE "user" SET hide_unrated_remote = 0 WHERE id = ?', (world.kid_id,)
        )
        connection.commit()
    finally:
        connection.close()
    for key in REMOTE_RATED:
        bind(database, FAMILY_SOURCE, f"r-{key}", world.seed.works[key], world.seed.editions[key])
    reduced = world.kid.get(SEARCH, params=ask)
    assert reduced.status_code == 200, reduced.text
    assert [i["title"] for i in reduced.json()["items"]] == [REMOTE_PLAIN]  # the rest is rated
    world.clean(reduced.text)
    key = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="kidkey2", username="kid", read_only=True)
        .value
    )
    keyed = world.env.other_client("10.0.0.53").get(
        SEARCH, params=ask, headers={"Authorization": f"Bearer {key}"}
    )
    assert keyed.status_code == 403
    world.clean(keyed.text)


def _gone(world: World, method: str, path_of: Any, body: Any = None) -> None:
    """The same 404 for a Work above the ceiling, unrated, not even rated as a whole, or absent."""
    absent = _call(world, method, path_of(MISSING), body)
    assert absent.status_code == 404
    for key in HIDDEN_FROM_CEILING_10:
        answer = _call(world, method, path_of(world.seed.works[key]), body)
        assert _shape(answer) == _shape(absent), key
        world.clean(answer.text)


def _call(world: World, method: str, path: str, body: Any) -> Any:
    headers = {CSRF_HEADER: world.env.csrf(world.kid)}
    if body is None:
        return world.kid.request(method, path, headers=headers)
    return world.kid.request(method, path, headers=headers, json=body)


def _surface_me_state_get(world: World) -> None:
    _gone(world, "GET", lambda w: f"{ME}/state/{w}")
    assert world.kid.get(f"{ME}/state/{world.seed.works['ok']}").status_code == 200


def _surface_me_state_put(world: World) -> None:
    _gone(world, "PUT", lambda w: f"{ME}/state/{w}", {"followed": True, "favorite": True})
    rows = world.seed.engine.connect().execute(sa.text("SELECT work_id FROM user_state")).all()
    assert rows == []  # nothing was written for a Work she may not see
    assert world.put(f"{ME}/state/{world.seed.works['ok']}", {"followed": True}).status_code == 200


def _surface_me_state_list(world: World) -> None:
    now = "2026-11-02 09:00:00"
    with world.seed.engine.begin() as connection:  # a flag set before a Work was re-rated upwards
        for key in HIDDEN_FROM_CEILING_10:
            connection.execute(
                sa.text(
                    "INSERT INTO user_state (user_id, work_id, followed, favorite, stamp) "
                    "VALUES (:u, :w, 1, 1, :t)"
                ),
                {"u": world.kid_id, "w": world.seed.works[key], "t": now},
            )
        connection.execute(
            sa.text(
                "INSERT INTO user_state (user_id, work_id, followed, favorite, stamp) "
                "VALUES (:u, :w, 1, 0, :t)"
            ),
            {"u": world.kid_id, "w": world.seed.works["ok"], "t": now},
        )
    answer = world.kid.get(f"{ME}/state")
    assert [i["work_id"] for i in answer.json()["items"]] == [world.seed.works["ok"]]
    world.clean(answer.text)


def _surface_me_title(world: World) -> None:
    _gone(world, "GET", lambda w: f"{ME}/works/{w}/title")
    assert world.kid.get(f"{ME}/works/{world.seed.works['ok']}/title").status_code == 200


def _surface_activity(world: World) -> None:
    from datetime import UTC, datetime

    with world.seed.engine.begin() as connection:
        for _key, work_id in world.seed.works.items():
            connection.execute(
                sa.insert(platform.event).values(
                    type="work.updated", schema_version=1, payload={"work_id": work_id},
                    occurred_at=datetime(2026, 11, 3, 9, 0, tzinfo=UTC), actor_id=world.kid_id,
                    visibility="household",
                )
            )  # fmt: skip
    answer = world.kid.get("/api/v1/activity", params={"limit": 100})
    assert answer.status_code == 200
    world.clean(answer.text)
    shown = {i["payload"]["work_id"] for i in answer.json()}
    assert shown == {world.seed.works[k] for k in VISIBLE_FOR_CEILING_10}


def _surface_works_since(world: World) -> None:
    """The list of Works and its removals are the child's own view: a page of visible Works, and
    a removal only for a Work she saw at the token. By session and by key, never the creator's."""
    first = world.kid.get("/api/v1/works", params={"limit": 200})
    assert first.status_code == 200
    world.clean(first.text)
    assert {i["title"] for i in first.json()["items"]} == {
        world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10
    }
    assert first.json()["total"] == len(VISIBLE_FOR_CEILING_10)
    assert world.kid.get("/api/v1/works", params={"limit": 200, "offset": 3}).json()["items"] == []
    # M3 L41, "Arrivé récemment": the newest-first order is the same view, never a wider one
    recent = world.kid.get("/api/v1/works", params={"limit": 200, "sort": "recent"})
    assert recent.status_code == 200
    world.clean(recent.text)
    assert {i["title"] for i in recent.json()["items"]} == {
        world.seed.title_of(k) for k in VISIBLE_FOR_CEILING_10
    }
    assert recent.json()["total"] == len(VISIBLE_FOR_CEILING_10)
    key = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="kidkey2", username="kid", read_only=True)
        .value
    )
    by_key = world.env.other_client("10.0.0.57").get(
        "/api/v1/works", params={"limit": 200}, headers={"Authorization": f"Bearer {key}"}
    )
    assert by_key.status_code == 200
    world.clean(by_key.text)
    assert {i["work_id"] for i in by_key.json()["items"]} == {
        i["work_id"] for i in first.json()["items"]
    }
    from shelfhold.catalog.application import changes

    token = first.json()["since"]
    with world.seed.engine.begin() as connection:  # hidden Works leave: she never saw them
        for key_name in ("above", "x", "unrated", "flagged"):
            changes.record_work_removed(connection, world.seed.works[key_name])
    answer = world.kid.get("/api/v1/works", params={"since": token, "limit": 200})
    assert answer.status_code == 200
    assert answer.json()["removed"] == []
    world.clean(answer.text)


def _surface_sse(world: World) -> None:
    """The stream is a projection like any other: the rows are the child's own household entries
    about every Work of the catalogue, and what the stream sends is what the filter lets through
    (the attacks on the stream itself are in `test_l12_events_sse.py`)."""
    from datetime import timedelta

    from tests.sse_env import cookie_header, open_stream

    def write() -> None:
        with world.seed.engine.begin() as connection:
            for work_id in world.seed.works.values():
                connection.execute(
                    sa.insert(platform.event).values(
                        type="work.updated", schema_version=1, payload={"work_id": work_id},
                        occurred_at=world.env.clock.now() - timedelta(seconds=1),
                        actor_id=world.kid_id, visibility="household",
                    )
                )  # fmt: skip

    with pytest.MonkeyPatch.context() as patch:
        received = open_stream(
            world.env.app, world.env.clock, patch, cookie=cookie_header(world.kid),
            script=[write], until=lambda r: len(r.heartbeats()) >= 1,
        )  # fmt: skip
    assert received.status == 200
    world.clean("\n".join(received.frames))
    shown = {event["payload"]["work_id"] for _, event in received.events()}
    assert shown == {world.seed.works[k] for k in VISIBLE_FOR_CEILING_10}


def _surface_notification(world: World) -> None:
    """A notification (and, later, an SSE frame) is a projection like any other: the one function
    that builds it asks the same filter whether the Work it is about is hers."""
    kid_scope = reader_scope(world.seed.engine, world.kid_id)
    viewer = Viewer(world.kid_id, "child")
    with world.seed.engine.connect() as connection:
        for key, work_id in world.seed.works.items():
            event = ActivityEvent(
                1, "work.updated", "household", {"work_id": work_id}, owner_id=world.kid_id,
                owner_role="child", title=world.seed.title_of(key),
            )  # fmt: skip
            item = project_event(connection, viewer, kid_scope, event)
            assert (item is not None) == (key in VISIBLE_FOR_CEILING_10), key
            if item is not None:
                assert item.title == world.seed.title_of(key)
        unlisted = ActivityEvent(
            2, "edition.filed", "household", {"edition_id": world.seed.editions["mixed_new"]},
            owner_id=world.kid_id, owner_role="child", title="Unrated edition of a rated Work",
        )  # fmt: skip
        assert project_event(connection, viewer, kid_scope, unlisted) is None
        # a payload that names a Work and a hidden one beside it is hidden as a whole
        both = ActivityEvent(
            3, "work.merged", "household",
            {"work_id": world.seed.works["ok"], "other_work_id": world.seed.works["unrated"]},
            owner_id=world.kid_id, owner_role="child", title="x",
        )  # fmt: skip
        assert project_event(connection, viewer, kid_scope, both) is None
        # the adult scope is not narrowed
        admin = project_event(
            connection, Viewer("a", "admin"), scope_of("admin", None),
            ActivityEvent(4, "work.updated", "ops", {"work_id": world.seed.works["unrated"]}),
        )  # fmt: skip
        assert admin is not None
    _notification_built_for_a_child(world)


def _notification_built_for_a_child(world: World) -> None:
    """L14: the real router and sender, in the shape of production: ONE shared channel that
    everybody reads. The child follows every Work of the catalogue, and so does an adult (who
    sees the hidden ones). What leaves on the whole channel, for anyone, names no title, no
    series, no identifier of a hidden Work; the child's own notice counts only what is hers."""
    from shelfhold.events.bus import EventBus
    from shelfhold.infrastructure.schema import identity
    from shelfhold.integrations.notify import NotificationRouter, NotificationSender
    from tests.auth_env import ManualClock

    engine, clock = world.seed.engine, ManualClock()
    sent: list[tuple[str, str]] = []

    def deliver(user_id: str, message: Any) -> bool:
        sent.append((user_id, f"{message.title}\n{message.body}"))
        return True

    bus = EventBus(clock)
    NotificationRouter(engine, clock).subscribe(bus)
    with engine.begin() as connection:
        adult = connection.execute(
            sa.select(identity.user.c.id).where(identity.user.c.role == "admin")
        ).scalar_one()
        connection.execute(sa.update(identity.user).values(notify_mode="immediate"))
        for who in (world.kid_id, adult):
            for work_id in world.seed.works.values():
                connection.execute(
                    sa.insert(identity.user_state).values(
                        user_id=who, work_id=work_id, followed=True
                    )
                )
    for work_id in world.seed.works.values():
        with engine.begin() as connection:
            published = bus.publish(
                connection, "unit.stored",
                {"unit_id": new_ulid(), "edition_id": new_ulid(), "work_id": work_id},
            )  # fmt: skip
        bus.deliver([published])
    NotificationSender(engine, clock, deliver=deliver, pending_withdrawals=lambda _c: 0).run_once()
    channel = "\n".join(t for _u, t in sent)  # everything, whoever it was for
    world.clean(channel)
    for key in (*VISIBLE_FOR_CEILING_10, *HIDDEN_FROM_CEILING_10):
        assert world.seed.title_of(key) not in channel, key
    kid = "\n".join(t for u, t in sent if u == world.kid_id)
    mine = "\n".join(t for u, t in sent if u == adult)
    assert f"{len(VISIBLE_FOR_CEILING_10)} new chapter" in kid  # the control: hers, counted
    assert f"{len(world.seed.works)} new chapter" in mine  # the adult is not narrowed


@pytest.mark.req("EF-USR-006", "ENF-SEC-010", "EF-USR-014")
@pytest.mark.parametrize("surface", SURFACES)
def test_unrated_invisible_to_child_on_all_surfaces(world: World, surface: str) -> None:
    """The acceptance of EF-USR-006: an unrated Work (or one above the ceiling) appears nowhere
    for a child, and nothing she can do tells her that it exists."""
    globals()[f"_surface_{surface}"](world)


# -- the scope is the library of the profile, not the shelf ------------------------------------


@pytest.mark.req("EF-USR-006", "EF-USR-015")
def test_visibility_scope_by_library_not_shelf(world: World) -> None:
    """What a child sees follows the age of the Work against her own ceiling (the library her
    profile opens), never the shelf an Edition happens to be filed on."""
    seed, ids = world.seed, world.seed.works
    with seed.engine.connect() as connection:
        shown = {k for k, w in ids.items() if work_is_visible(connection, scope_of("child", 10), w)}
    assert shown == VISIBLE_FOR_CEILING_10
    # a rated Work on an adult shelf (no ceiling) is hers; the shelf does not hide it
    assert "adult_shelf" in shown
    # an unrated Work misfiled on the family shelf is still not hers: the shelf does not show it
    assert "misfiled" not in shown and "unrated" not in shown
    # a shelf whose `allowed_profiles` names someone else does not take a Work away from her
    assert "ok" in shown
    # a Work on two shelves is one Work: listed and counted once
    with seed.engine.connect() as connection:
        assert count_visible_works(connection, scope_of("child", 10)) == 3
        assert len(list_visible_works(connection, scope_of("child", 10), limit=50, offset=0)) == 3
    # the ceiling is hers: a 13+ child sees the 12+ Work, and a 6+ child sees only the 6+ ones
    with seed.engine.connect() as connection:
        thirteen = {
            w for w in ids.values() if work_is_visible(connection, scope_of("child", 13), w)
        }
        six = {w for w in ids.values() if work_is_visible(connection, scope_of("child", 6), w)}
        zero = {w for w in ids.values() if work_is_visible(connection, scope_of("child", 0), w)}
    assert thirteen == {ids[k] for k in (*VISIBLE_FOR_CEILING_10, "twelve")}
    assert six == {ids[k] for k in VISIBLE_FOR_CEILING_10}
    assert zero == set()  # 0 is the strictest ceiling, never "no ceiling"


@pytest.mark.req("EF-USR-006")
def test_filter_is_applied_in_the_query_not_after(world: World) -> None:
    """Pagination and counts come from the same predicate: a page boundary or a total cannot
    carry a hidden Work out."""
    seed = world.seed
    child = scope_of("child", 10)
    with seed.engine.connect() as connection:
        every = {w for w in seed.works.values()}
        assert visible_work_ids(connection, child, every) == {
            seed.works[k] for k in VISIBLE_FOR_CEILING_10
        }
        assert visible_work_ids(connection, child, {MISSING, ULID}) == set()
        assert visible_work_ids(connection, child, set()) == set()
        walked: list[str] = []
        for offset in range(0, 6):
            walked += [
                h.work_id for h in list_visible_works(connection, child, limit=1, offset=offset)
            ]
        assert sorted(walked) == sorted(seed.works[k] for k in VISIBLE_FOR_CEILING_10)
        assert list_visible_works(connection, child, limit=10, offset=3) == []
        assert list_visible_works(connection, child, limit=-5, offset=0) == []
        assert list_visible_works(connection, child, limit=10, offset=-1) == []
        assert len(list_visible_works(connection, child, limit=10**9, offset=0)) == 3
        assert count_visible_works(connection, child) == 3
        # the adult sees all, the unknown profile sees nothing
        assert count_visible_works(connection, scope_of("member", None)) == len(seed.works)
        assert count_visible_works(connection, scope_of("ghost", None)) == 0
        assert list_visible_works(connection, scope_of("ghost", None), limit=10, offset=0) == []
        assert visible_work_ids(connection, scope_of("ghost", None), every) == set()
        # the Editions: rated, not above, and of a Work she may see
        editions = visible_edition_ids(connection, child, set(seed.editions.values()))
        assert editions == {
            seed.editions[k] for k in ("ok", "ok_second", "adult_shelf", "mixed_rated")
        }
        assert visible_edition_ids(
            connection, scope_of("admin", None), set(seed.editions.values())
        ) == set(seed.editions.values())
        assert (
            visible_edition_ids(connection, scope_of("ghost", None), set(seed.editions.values()))
            == set()
        )


@pytest.mark.req("EF-USR-006")
def test_ceiling_lowered_takes_effect_on_the_next_request(world: World) -> None:
    """The ceiling is read from the database on every request, never kept in the session."""
    first = world.kid.get(SEARCH, params={"q": "Tale", "limit": 200})
    assert len(_titles(first)) == 3
    world.env.app.state.auth_gate.auth.profile_set(world.kid_id, max_age_rating=0)  # type: ignore[attr-defined]
    again = world.kid.get(SEARCH, params={"q": "Tale", "limit": 200})
    assert again.status_code == 200 and _titles(again) == []
    assert world.kid.get(f"{ME}/state/{world.seed.works['ok']}").status_code == 404


@pytest.mark.req("EF-USR-006")
def test_reader_scope_comes_from_the_profile_row(world: World) -> None:
    engine = world.seed.engine
    assert reader_scope(engine, world.kid_id) == scope_of("child", 10)
    assert reader_scope(engine, MISSING).kind == "none"  # unknown: nothing
    admin_id = str(
        engine.connect().execute(sa.text("SELECT id FROM user WHERE role='admin'")).scalar()
    )
    assert reader_scope(engine, admin_id).unrestricted
    assert reader_scope(engine, None).kind == "none"  # type: ignore[arg-type]


@pytest.mark.req("EF-USR-006")
def test_adults_search_the_whole_catalogue_children_do_not(world: World) -> None:
    mum = add_profile(world.env, "mum", "member", "10.0.0.3")
    assert len(mum.get(SEARCH, params={"q": "Tale", "limit": 200}).json()["items"]) == 11
    # `scope=all` stays the administrator's: a child has the family search, a member nothing
    assert mum.get(SEARCH, params={"q": "Tale", "scope": "all"}).status_code == 403
