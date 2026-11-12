"""Adversarial review of PR #204 (M3 L09): proofs of concept, red on purpose.

Each test below is a leak or a bypass of the parental barrier found in review. They are expected
to FAIL on the branch as it stands; each one turns green when its objection is fixed (see
`docs/decisions/M3.md`, "L09 : contradicteur de la #204 (Opus)"). The production code is not
touched here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import APIRouter
from fastapi.testclient import TestClient

from shelfhold.catalog.public import (
    Admission,
    RatingService,
    search_page,
    visible_edition_ids,
    work_is_visible,
)
from shelfhold.config import Settings
from shelfhold.identity.public import local_auth, scope_of
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, platform
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, run_cli
from tests.integration.test_child_surfaces import SEARCH, _routes
from tests.visibility_env import Seed, add_profile, seed_catalogue

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


class _World:
    def __init__(self, env: AuthEnv, seed: Seed, kid: TestClient, kid_id: str) -> None:
        self.env, self.seed, self.kid, self.kid_id = env, seed, kid, kid_id


@pytest.fixture
def poc_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as built:
        yield built


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, poc_env: AuthEnv) -> Iterator[_World]:
    seed = seed_catalogue(poc_env)
    kid = add_profile(poc_env, "kid", "child", "10.0.0.2", max_age_rating=10)
    assert run_cli(monkeypatch, poc_env.settings, "search", "rebuild") == 0
    assert poc_env.login().status_code == 200
    with seed.engine.connect() as connection:
        kid_id = str(
            connection.execute(sa.text("SELECT id FROM user WHERE username='kid'")).scalar()
        )
    yield _World(poc_env, seed, kid, kid_id)
    seed.engine.dispose()


def _titles(response: Any) -> list[str]:
    return [hit["title"] for hit in response.json()["items"]]


def _suggest(seed: Seed, entity_type: str, entity_id: str, value: int, *, x: bool = False) -> None:
    with seed.engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.age_suggestion).values(
                id=new_ulid(), entity_type=entity_type, entity_id=entity_id, value=value, x=x,
                provenance="provider", seen_at=NOW, state="open",
            )
        )  # fmt: skip


# -- O1: an open age suggestion on the Work is ignored by the filter ---------------------------


@pytest.mark.req("EF-USR-006", "EF-BIB-055")
def test_review_poc_work_level_suggestion_hides_work_from_child(world: _World) -> None:
    """A provider (or ComicInfo, OPF, a sub-source) says 18+ for a Work an admin rated 6+. The
    shelf admission already blocks every Edition of it (`RatingService._facts` reads the Work's
    suggestions); the visibility filter only reads Edition suggestions, so the child still finds
    the Work, its title and its Editions."""
    seed = world.seed
    _suggest(seed, "work", seed.works["ok"], 18, x=True)
    with seed.engine.connect() as connection:
        verdict = RatingService().admission_of(
            connection, seed.editions["ok"], shelf_id=seed.shelves["family"]
        )
        assert verdict is Admission.SUGGESTION_ABOVE  # the shelf already says "no"
        child = scope_of("child", 10)
        assert not work_is_visible(connection, child, seed.works["ok"]), "Work still visible"
        assert seed.editions["ok"] not in visible_edition_ids(
            connection, child, [seed.editions["ok"]]
        )
    answer = world.kid.get(SEARCH, params={"q": "Orchard"})
    assert _titles(answer) == [], answer.text


@pytest.mark.req("EF-USR-006", "EF-BIB-055")
def test_review_poc_suggestion_above_rating_under_ceiling(world: _World) -> None:
    """EF-BIB-055: a suggested age that exceeds the RATING blocks the Edition ("À classer"). The
    filter only hides it when the suggestion exceeds the child's CEILING: a 6+ Edition with an
    open 10+ suggestion is blocked from every family shelf but shown to a 10+ child."""
    seed = world.seed
    _suggest(seed, "edition", seed.editions["ok"], 10)
    with seed.engine.connect() as connection:
        verdict = RatingService().admission_of(
            connection, seed.editions["ok"], shelf_id=seed.shelves["family"]
        )
        assert verdict is Admission.SUGGESTION_ABOVE
        assert seed.editions["ok"] not in visible_edition_ids(
            connection, scope_of("child", 10), [seed.editions["ok"]]
        ), "an Edition waiting in 'À classer' is shown to the child"


# -- O2: a child's API key reads the activity as the integration role --------------------------


@pytest.mark.req("EF-USR-006", "EF-USR-016", "ENF-SEC-010")
def test_review_poc_child_key_never_sees_more_activity_than_child(world: _World) -> None:
    """`viewer_of` turns any key into the integration role: a child's key then receives every
    `ops` event of the household (codes and identifiers of what the adults acquire, export,
    purge) and a sibling's `household` entries, none of which her session sees. The scope filter
    only reads `work/edition/series/unit` ids, so an `export_item_id`, a `variant_id` or a
    `source_item_id` of a hidden Work passes."""
    seed, env = world.seed, world.env
    sib = add_profile(env, "sib", "child", "10.0.0.9", max_age_rating=10)
    assert sib is not None
    with seed.engine.begin() as connection:
        sib_id = str(
            connection.execute(sa.text("SELECT id FROM user WHERE username='sib'")).scalar()
        )
        connection.execute(
            sa.insert(platform.event).values(
                type="export.item_written", schema_version=1,
                payload={"export_item_id": new_ulid(), "destination_id": new_ulid()},
                occurred_at=NOW, actor_id=None, visibility="ops",
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(platform.event).values(
                type="work.updated", schema_version=1, payload={"work_id": seed.works["ok"]},
                occurred_at=NOW, actor_id=sib_id, visibility="household",
            )
        )  # fmt: skip
    key = (
        local_auth(seed.engine, env.settings)
        .create_api_key(name="kidkey", username="kid", read_only=True)
        .value
    )
    by_session = world.kid.get("/api/v1/activity", params={"limit": 100})
    by_key = env.other_client("10.0.0.53").get(
        "/api/v1/activity", params={"limit": 100}, headers={"Authorization": f"Bearer {key}"}
    )
    assert by_session.status_code == 200
    seen_by_session = {i["event_id"] for i in by_session.json()}
    seen_by_key = {i["event_id"] for i in by_key.json()} if by_key.status_code == 200 else set()
    assert seen_by_key <= seen_by_session, (
        f"a child's key sees {sorted(seen_by_key - seen_by_session)} her session does not"
    )


# -- O3: a search for a hidden title does more work than one for an absent title ---------------


@pytest.mark.req("EF-USR-006")
def test_review_poc_hidden_and_absent_search_cost_the_same(world: _World) -> None:
    """`search_page` returns at once when no index row matches, and only otherwise reads the
    language of every Edition and runs the filtered query: a hidden title costs two more full
    statements than an absent one, a delay that tells "hidden" from "absent" (a title oracle)."""
    seed = world.seed
    statements: list[str] = []

    def count(*args: Any) -> None:
        statements.append(str(args[2]))

    child = scope_of("child", 10)
    sa.event.listen(seed.engine, "before_cursor_execute", count)
    try:
        with seed.engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))  # the BEGIN of the connection is not the search
            statements.clear()
            search_page(connection, "Harbor Sixteen", scope=child)
            hidden = len(statements)
            statements.clear()
            search_page(connection, "Harbor Zzzqxw", scope=child)
            absent = len(statements)
    finally:
        sa.event.remove(seed.engine, "before_cursor_execute", count)
    assert hidden == absent, f"hidden title: {hidden} statements, absent title: {absent}"


# -- O4: the meta-test is blind to routes outside the OpenAPI document --------------------------


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_review_poc_meta_test_sees_routes_outside_openapi(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """SSE streams, thumbnails and covers are the surfaces most often declared with
    `include_in_schema=False`. Such a route serves a child with no child case and the meta-test
    (`_routes`, built from `app.openapi()`) never sees it."""
    extra = APIRouter()

    @extra.get("/api/v1/zz-stream", include_in_schema=False)
    def stream() -> dict[str, str]:
        return {"title": "Tale of Harbor Sixteen"}

    with auth_env(monkeypatch, settings, routers=[extra]) as built:
        assert "GET /api/v1/zz-stream" in _routes(built.app), (
            "a route outside the OpenAPI document escapes the meta-test"
        )


# -- O5: `allowed_profiles` is not read -------------------------------------------------------


@pytest.mark.skip(
    reason="decision of the coordinator (M4, docs/decisions/M3.md): `allowed_profiles` rules the "
    "export to Kavita (L10), not the visibility in Shelfhold; EF-USR-006 says by library"
)
@pytest.mark.req("EF-USR-006", "EF-USR-015")
def test_review_poc_shelf_restricted_to_another_child_is_not_hers(
    monkeypatch: pytest.MonkeyPatch, world: _World
) -> None:
    """A parent restricts a family shelf to one child (`shelf.allowed_profiles`, a reserved
    parental-control action with freshness). A Work filed only there is shown to every other
    child on every Shelfhold surface, although her Kavita account never opens that library."""
    seed, env = world.seed, world.env
    add_profile(env, "sib", "child", "10.0.0.9", max_age_rating=10)
    with seed.engine.begin() as connection:
        sib_id = str(
            connection.execute(sa.text("SELECT id FROM user WHERE username='sib'")).scalar()
        )
        kind = str(connection.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one())
        shelf_id = new_ulid()
        connection.execute(
            sa.insert(export.shelf).values(
                id=shelf_id, name="Sib only", phrase="sib only", max_age_rating=10,
                allowed_profiles=[sib_id],
            )
        )  # fmt: skip
        work_id = new_ulid()
        connection.execute(
            sa.insert(catalog.work).values(
                id=work_id, kind=kind, title="Tale of Sibling Shelf", sort_title="tale of sib",
                age_rating=6, age_x=False, rated_at=NOW, is_adult=False,
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(catalog.edition).values(
                id=new_ulid(), work_id=work_id, lang="en", translation_type="official",
                media="images", rated_at=NOW, shelf_id=shelf_id,
            )
        )  # fmt: skip
    assert run_cli(monkeypatch, env.settings, "search", "rebuild") == 0  # index the new Work
    answer = world.kid.get(SEARCH, params={"q": "Sibling"})
    assert _titles(answer) == [], "a Work on a shelf allowed to another child reaches her"


# -- O6: "Ajouter directement" is a second door to `shelf.choose_family` -----------------------


def _post(env: AuthEnv, client: TestClient, path: str, body: Any) -> Any:
    return client.post(path, json=body, headers={CSRF_HEADER: env.csrf(client)})


@pytest.mark.req("EF-BIB-055", "EF-USR-022", "ENF-SEC-010")
def test_review_poc_add_directly_honours_choose_family_freshness(world: _World) -> None:
    """With a child profile, choosing a family shelf (`shelf.choose_family`, PARENTAL_CONTROL)
    needs a session younger than 900 s. `POST /editions/{id}/shelf` asks for it; the new
    `POST /editions/{id}/add-directly` files the same Edition on the same family shelf with a
    two-hour-old session, without `reauth-required`."""
    seed, env = world.seed, world.env
    family, edition = seed.shelves["family"], seed.editions["twelve"]
    env.clock.advance(timedelta(hours=2))
    by_reserved = _post(env, env.client, f"/api/v1/editions/{edition}/shelf", {"shelf_id": family})
    assert by_reserved.status_code == 401, by_reserved.text  # the reserved door asks
    by_add = _post(
        env, env.client, f"/api/v1/editions/{edition}/add-directly", {"shelf_id": family}
    )
    assert by_add.status_code == 401, f"add-directly filed without re-auth: {by_add.text}"


@pytest.mark.req("EF-BIB-055", "EF-BIB-036", "ENF-SEC-010")
def test_review_poc_member_cannot_choose_family_shelf_by_add_directly(world: _World) -> None:
    """`shelf.choose_family` is the admin's (DEFAULT_RESERVED_ROLES). A member who may classify
    is refused on `POST /editions/{id}/shelf` but files the same Edition on the family shelf
    through add-directly: the truth table holds two different answers for one action."""
    seed, env = world.seed, world.env
    family, edition = seed.shelves["family"], seed.editions["twelve"]
    rater = add_profile(env, "rater", "member", "10.0.0.4", can_rate=True)
    by_reserved = _post(env, rater, f"/api/v1/editions/{edition}/shelf", {"shelf_id": family})
    assert by_reserved.status_code == 403
    by_add = _post(env, rater, f"/api/v1/editions/{edition}/add-directly", {"shelf_id": family})
    assert not by_add.json().get("filed"), f"member chose a family shelf: {by_add.text}"
