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
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

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
    "POST /api/v1/auth/reauth",
    "GET /api/v1/auth/session",
    "GET /api/v1/me/preferences",
    "PATCH /api/v1/me/preferences",
    "GET /api/v1/me/title-languages",
    "PUT /api/v1/me/title-languages",
    "GET /api/v1/me/visibility",
    "PUT /api/v1/me/visibility",
)
OPEN = {
    "GET /api/v1/activity": "activity",
    "GET /api/v1/me/state": "me_state_list",
    "GET /api/v1/me/state/{work_id}": "me_state_get",
    "PUT /api/v1/me/state/{work_id}": "me_state_put",
    "GET /api/v1/me/works/{work_id}/title": "me_title",
    "GET /api/v1/search": "search",
}
CLOSED = (
    "GET /api/v1/openapi.json",
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
    "PATCH /api/v1/profiles/{profile_id}",
    "GET /api/v1/profiles/{profile_id}/source-policies",
    "PUT /api/v1/profiles/{profile_id}/source-policies/{source_key}",
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
    "POST /api/v1/shelves",
    "POST /api/v1/shelves/precreate",
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
)

# The one route whose body is read before its guard answers: it is given a valid one, so that the
# refusal under test is the child's, not a validation error.
BODIES = {"POST /api/v1/reconcile/forget-root": {"root_id": "x", "confirm": "x"}}

# Surfaces that do not exist yet: the lot that builds each one adds its child case, and this
# list is where the suite notices that the surface has arrived (the meta-test fails when a path
# with that word shows up in the OpenAPI document).
PENDING_SURFACES = {
    "sse": ("L12", ("stream", "events")),
    "thumbnails": ("L24", ("thumb", "thumbs", "thumbnail", "thumbnails")),
    "covers": ("L24", ("cover", "covers")),
    "notifications": ("L14", ("notification", "notifications")),
    "opds": ("M5", ("opds",)),
}

SURFACES = (
    "search", "search_pages", "search_key", "federated", "me_state_get", "me_state_put",
    "me_state_list", "me_title", "activity", "notification",
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


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
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
    """Every route of the application, the ones left out of the OpenAPI document included: a
    stream, a thumbnail or a cover is usually declared `include_in_schema=False`."""
    return {
        f"{m} {context.path}"
        for context in iter_route_contexts(app.routes)  # routers included lazily are walked too
        if context.path is not None
        for m in (context.methods or set()) - {"HEAD", "OPTIONS"}
    }


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
    """A remote result has no rating: the federated search is not a child's door (L19)."""
    answer = world.kid.get(SEARCH, params={"q": "Tale", "scope": "all"})
    assert answer.status_code == 403
    world.clean(answer.text)


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
    # `scope=all` stays the administrator's until the family search (L19)
    assert mum.get(SEARCH, params={"q": "Tale", "scope": "all"}).status_code == 403
