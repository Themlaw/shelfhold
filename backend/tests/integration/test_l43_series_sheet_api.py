"""M3 L43: the doors the series sheet uses, over the real application (real sessions, CSRF, ops).

The sheet is the admin's: it tells what the server allows (`can`), and every write it offers is
attacked here with a child, a member, a key and a wrong body. Nothing here replaces a service by a
fake: a refusal is the real refusal, and each write is read back from the database.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env
from tests.visibility_env import Seed, add_profile, seed_catalogue

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
MISSING = "01HZZZZZZZZZZZZZZZZZZZZZZM"
FOLLOWED = "ok"  # the Edition of the seed the sheet is read on


@dataclass
class World:
    env: AuthEnv
    seed: Seed
    kid: TestClient
    member: TestClient
    edition: str
    bindings: list[str]  # the active ones, in rank order
    inactive: str
    teams: list[str]

    @property
    def sheet_path(self) -> str:
        return f"/api/v1/editions/{self.edition}/sheet"

    def sheet(self) -> dict[str, Any]:
        got = self.env.client.get(self.sheet_path)
        assert got.status_code == 200, got.text
        return got.json()

    def post(self, path: str, body: Any = None, *, client: TestClient | None = None) -> Any:
        return self.env.post(path, json=body if body is not None else {}, client=client)

    def key(self) -> str:
        auth = local_auth(self.seed.engine, self.env.settings)
        return auth.create_api_key(name="sheetkey", username="admin", read_only=False).value

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.seed.engine.connect() as connection:
            return [dict(r._mapping) for r in connection.execute(sa.text(sql), params)]


def _seed_follow(seed: Seed, edition: str) -> tuple[list[str], str, list[str]]:
    work = seed.works["ok"]
    bindings: list[str] = []
    with seed.engine.begin() as connection:
        for rank, remote in enumerate(("alpha", "beta", "gamma"), start=1):
            item = new_ulid()
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=item, plugin_id="plug", source_key=f"src-{remote}", remote_id=remote
                )
            )
            binding = new_ulid()
            bindings.append(binding)
            connection.execute(
                sa.insert(acquisition.source_binding).values(
                    id=binding, work_id=work, edition_id=edition, source_item_id=item,
                    priority=rank, active=True,
                )
            )  # fmt: skip
        item = new_ulid()
        connection.execute(
            sa.insert(acquisition.source_item).values(
                id=item, plugin_id="plug", source_key="src-delta", remote_id="delta"
            )
        )
        inactive = new_ulid()
        connection.execute(
            sa.insert(acquisition.source_binding).values(
                id=inactive, work_id=work, edition_id=edition, source_item_id=item, priority=9,
                active=False, disabled_reason="removed_upstream",
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(acquisition.series_follow).values(edition_id=edition, work_id=work)
        )
        for number in ("3", "7"):
            connection.execute(
                sa.insert(acquisition.series_gap).values(
                    edition_id=edition,
                    track="main",
                    number_segment="",
                    number_norm=number,
                    first_seen_at=NOW,
                )
            )
        teams = []
        for rank, label in enumerate(("team one", "team two"), start=1):
            team = new_ulid()
            teams.append(team)
            connection.execute(
                sa.insert(catalog.team).values(id=team, edition_id=edition, label=label)
            )
            connection.execute(
                sa.insert(catalog.team_priority).values(edition_id=edition, team_id=team, rank=rank)
            )
    return bindings, inactive, teams


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with auth_env(monkeypatch, settings) as env:
        assert env.login().status_code == 200
        seed = seed_catalogue(env)
        kid = add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
        member = add_profile(env, "mum", "member", "10.0.0.3")
        edition = seed.editions[FOLLOWED]
        bindings, inactive, teams = _seed_follow(seed, edition)
        yield World(env, seed, kid, member, edition, bindings, inactive, teams)
        seed.engine.dispose()


def _ranks(world: World) -> dict[str, int]:
    return {
        r["id"]: r["priority"]
        for r in world.rows("SELECT id, priority FROM source_binding WHERE edition_id=:e",
                            e=world.edition)
    }  # fmt: skip


# -- the sheet is the admin's ------------------------------------------------------------------
@pytest.mark.req("EF-SRC-037")
def test_series_sheet_reorders_sources_and_shows_inactive_reason(world: World) -> None:
    shown = world.sheet()
    assert [b["id"] for b in shown["bindings"]][:3] == world.bindings
    assert [b["rank"] for b in shown["bindings"]][:3] == [1, 2, 3]
    (inactive,) = [b for b in shown["bindings"] if not b["active"]]
    assert inactive["id"] == world.inactive
    assert inactive["disabled_reason"] == "removed_upstream"
    assert shown["can"]["reorder"] is True

    reversed_ids = list(reversed(world.bindings))
    done = world.post(f"/api/v1/editions/{world.edition}/bindings/order",
                      {"binding_ids": reversed_ids})  # fmt: skip
    assert done.status_code == 200, done.text
    after = world.sheet()
    assert [b["id"] for b in after["bindings"] if b["active"]] == reversed_ids
    assert [b["rank"] for b in after["bindings"] if b["active"]] == [1, 2, 3]
    assert _ranks(world)[world.inactive] == 9  # a disabled binding keeps its place and its reason


@pytest.mark.req("EF-SRC-037")
@pytest.mark.parametrize(
    "mutate",
    ("missing", "duplicate", "inactive", "foreign", "empty"),
)
def test_reorder_refuses_anything_but_the_active_bindings_once_and_moves_nothing(
    world: World, mutate: str
) -> None:
    before = _ranks(world)
    ids = list(reversed(world.bindings))
    bad = {
        "missing": ids[:2],
        "duplicate": [ids[0], ids[0], ids[1]],
        "inactive": [*ids, world.inactive],
        "foreign": [*ids[:2], new_ulid()],
        "empty": [],
    }[mutate]
    got = world.post(f"/api/v1/editions/{world.edition}/bindings/order", {"binding_ids": bad})
    assert got.status_code in (400, 422), got.text
    assert _ranks(world) == before


@pytest.mark.req("EF-SRC-037")
def test_reorder_of_another_editions_bindings_is_refused(world: World) -> None:
    other = world.seed.editions["twelve"]
    got = world.post(
        f"/api/v1/editions/{other}/bindings/order", {"binding_ids": list(world.bindings)}
    )
    assert got.status_code in (400, 404, 422)
    assert [b["rank"] for b in world.sheet()["bindings"] if b["active"]] == [1, 2, 3]
    unknown = world.post(
        f"/api/v1/editions/{MISSING}/bindings/order", {"binding_ids": list(world.bindings)}
    )
    assert unknown.status_code == 404


@pytest.mark.req("EF-SRC-037")
def test_the_sheet_offers_only_what_the_server_allows(world: World) -> None:
    other = world.seed.editions["twelve"]  # no binding, no follow, one team: nothing to reorder
    bare = world.env.client.get(f"/api/v1/editions/{other}/sheet").json()
    assert bare["bindings"] == [] and bare["followed"] is False
    assert bare["can"]["check"] is False
    assert bare["can"]["reorder"] is False
    assert bare["can"]["acknowledge_gap"] is False
    assert bare["can"]["separate"] is False
    assert bare["can"]["confirm_attach"] is False
    full = world.sheet()["can"]
    assert full["check"] and full["reorder"] and full["acknowledge_gap"] and full["prefer_team"]
    with world.seed.engine.begin() as connection:  # a paused series is not looked at on demand
        connection.execute(
            sa.update(acquisition.series_follow)
            .where(acquisition.series_follow.c.edition_id == world.edition)
            .values(check_override="paused")
        )
    assert world.sheet()["can"]["check"] is False


@pytest.mark.req("EF-SRC-037", "EF-USR-006")
@pytest.mark.parametrize(
    ("verb", "path", "body"),
    [
        ("get", "/api/v1/editions/{e}/sheet", None),
        ("post", "/api/v1/editions/{e}/bindings/order", {"binding_ids": ["x"]}),
        ("post", "/api/v1/editions/{e}/upstream-status", {"status": "finished"}),
        ("post", "/api/v1/editions/{e}/preferred-team", {"team_id": "x"}),
        ("post", "/api/v1/editions/{e}/gaps/ack", {"number": "3"}),
        ("post", "/api/v1/editions/{e}/check", None),
        ("post", "/api/v1/attachments/{e}/plan", None),
        ("post", "/api/v1/attachments/{e}/confirm", {"confirm": "x"}),
    ],
)
def test_a_child_and_a_member_are_refused_on_every_door_of_the_sheet(
    world: World, verb: str, path: str, body: Any
) -> None:
    before = (_ranks(world), world.rows("SELECT * FROM gap_ack"),
              world.rows("SELECT upstream_status, upstream_status_by FROM edition"),
              world.rows("SELECT * FROM team_priority"))  # fmt: skip
    url = path.format(e=world.edition)
    for who in (world.kid, world.member):
        got = who.get(url) if verb == "get" else world.env.post(url, client=who, json=body or {})
        assert got.status_code == 403, (path, got.text)
        assert world.edition not in got.text
    anonymous = TestClient(world.env.app, base_url="https://testserver")
    unauth = anonymous.get(url) if verb == "get" else anonymous.post(url, json=body or {})
    assert unauth.status_code in (401, 403)
    after = (_ranks(world), world.rows("SELECT * FROM gap_ack"),
             world.rows("SELECT upstream_status, upstream_status_by FROM edition"),
             world.rows("SELECT * FROM team_priority"))  # fmt: skip
    assert after == before


@pytest.mark.req("EF-SRC-037")
def test_a_write_of_the_sheet_needs_the_csrf_token_of_the_session(world: World) -> None:
    got = world.env.client.post(
        f"/api/v1/editions/{world.edition}/upstream-status",
        json={"status": "finished"},
        headers={CSRF_HEADER: "not-the-token"},
    )
    assert got.status_code in (400, 403)
    assert world.sheet()["upstream_status"] == "unknown"


@pytest.mark.req("EF-BIB-020")
def test_the_sheet_carries_the_cover_address_and_refuses_what_is_no_identifier(
    world: World,
) -> None:
    from tests.thumbnail_env import put_cover

    assert world.sheet()["cover_hash"] is None
    with world.seed.engine.begin() as connection:
        digest = put_cover(world.env.settings.data_dir, connection, world.edition, (9, 90, 9))
    assert world.sheet()["cover_hash"] == digest
    assert world.env.client.get("/api/v1/editions/not-an-id/sheet").status_code in (400, 422)
    assert world.env.client.get(f"/api/v1/editions/{MISSING}/sheet").status_code == 404


# -- check now ---------------------------------------------------------------------------------
@pytest.mark.req("EF-SRC-038")
def test_check_now_button_calls_check(world: World) -> None:
    """The one door `follow check` has: the sheet offers the button (`can.check`) and the route
    answers a problem, never a 500, for an Edition nobody follows or that does not exist."""
    assert world.sheet()["can"]["check"] is True
    unknown = world.post(f"/api/v1/editions/{MISSING}/check")
    assert unknown.status_code in (404, 409, 503)
    not_followed = world.post(f"/api/v1/editions/{world.seed.editions['twelve']}/check")
    assert not_followed.status_code in (404, 409, 503)


# -- gaps --------------------------------------------------------------------------------------
@pytest.mark.req("EF-BIB-056")
def test_gap_acknowledge_from_sheet(world: World) -> None:
    assert [g["number"] for g in world.sheet()["gaps"]] == ["3", "7"]
    first = world.sheet()["gaps"][0]
    body = {"number": first["number"], "track": first["track"], "segment": first["segment"]}
    done = world.post(f"/api/v1/editions/{world.edition}/gaps/ack", body)
    assert done.status_code == 200, done.text
    assert done.json()["changed"] is True
    assert [g["number"] for g in world.sheet()["gaps"]] == ["7"]
    (ack,) = world.rows("SELECT track, number_segment, number_norm, acked_by FROM gap_ack")
    assert (ack["track"], ack["number_segment"], ack["number_norm"]) == ("main", "", "3")
    assert ack["acked_by"] is not None  # who said so
    replay = world.post(f"/api/v1/editions/{world.edition}/gaps/ack", body)
    assert replay.json()["changed"] is False
    undone = world.post(f"/api/v1/editions/{world.edition}/gaps/ack", {**body, "undo": True})
    assert undone.status_code == 200
    assert world.rows("SELECT * FROM gap_ack") == []  # taken back; the next look shows it again
    assert [g["number"] for g in world.sheet()["gaps"]] == ["7"]


@pytest.mark.req("EF-BIB-056")
@pytest.mark.parametrize(
    "body",
    (
        {"number": "99"},  # nobody listed it
        {"number": "3.5"},  # decimals are never gaps
        {"number": "x"},
        {"number": "3", "track": "bogus"},
        {"number": "3", "segment": "2"},  # a gap of another segment
        {"number": "-3"},
    ),
)
def test_gap_ack_refuses_what_the_sheet_does_not_show(world: World, body: dict[str, str]) -> None:
    got = world.post(f"/api/v1/editions/{world.edition}/gaps/ack", body)
    assert got.status_code in (400, 404, 422), got.text
    assert world.rows("SELECT * FROM gap_ack") == []
    assert [g["number"] for g in world.sheet()["gaps"]] == ["3", "7"]


# -- manual edit and the lock ------------------------------------------------------------------
@pytest.mark.req("EF-BIB-011")
def test_manual_edit_from_sheet_locks_field(world: World) -> None:
    done = world.post(
        f"/api/v1/editions/{world.edition}/fields/title/lock",
        {"value": "A title by hand", "lang": "en", "on": "edition"},
    )
    assert done.status_code == 200, done.text
    (field,) = [
        f for f in world.sheet()["fields"] if f["field"] == "title" and f["on"] == "edition"
    ]
    assert field["value"] == "A title by hand"
    assert field["locked"] is True and field["manual"] is True
    (stored,) = world.rows(
        "SELECT locked, provenance FROM metadata_field WHERE entity_id=:e AND field='title'",
        e=world.edition,
    )
    assert stored["locked"] and stored["provenance"] == "manual"
    for who in (world.kid, world.member):
        refused = world.env.post(
            f"/api/v1/editions/{world.edition}/fields/title/lock", client=who,
            json={"value": "mine"},
        )  # fmt: skip
        assert refused.status_code == 403
    assert world.sheet()["fields"][0]["value"] == "A title by hand"


# -- upstream status, by hand (D23) ------------------------------------------------------------
@pytest.mark.req("EF-SRC-038")
def test_upstream_status_is_set_by_hand_and_says_so(world: World) -> None:
    assert world.sheet()["upstream_status"] == "unknown"
    done = world.post(f"/api/v1/editions/{world.edition}/upstream-status", {"status": "finished"})
    assert done.status_code == 200, done.text
    after = world.sheet()
    assert after["upstream_status"] == "finished"
    assert after["upstream_status_by"] == "manual"
    (row,) = world.rows("SELECT upstream_status_by, upstream_status_at FROM edition WHERE id=:e",
                        e=world.edition)  # fmt: skip
    assert row["upstream_status_by"] == "manual" and row["upstream_status_at"] is not None


@pytest.mark.req("EF-SRC-038")
@pytest.mark.parametrize("status", ("done", "", "FINISHED", "provider"))
def test_upstream_status_refuses_a_value_outside_the_list(world: World, status: str) -> None:
    got = world.post(f"/api/v1/editions/{world.edition}/upstream-status", {"status": status})
    assert got.status_code in (400, 422)
    assert world.sheet()["upstream_status"] == "unknown"
    assert world.post(
        f"/api/v1/editions/{MISSING}/upstream-status", {"status": "finished"}
    ).status_code == 404  # fmt: skip


# -- preferred team ----------------------------------------------------------------------------
@pytest.mark.req("EF-BIB-005")
def test_preferred_team_goes_first_and_nothing_served_is_replaced(world: World) -> None:
    shown = world.sheet()["teams"]
    assert [t["id"] for t in shown] == world.teams and shown[0]["preferred"] is True
    done = world.post(f"/api/v1/editions/{world.edition}/preferred-team",
                      {"team_id": world.teams[1]})  # fmt: skip
    assert done.status_code == 200, done.text
    after = world.sheet()["teams"]
    assert [t["id"] for t in after] == [world.teams[1], world.teams[0]]
    assert [t["preferred"] for t in after] == [True, False]
    assert world.rows("SELECT * FROM unit_variant") == []  # a choice is not an upgrade


@pytest.mark.req("EF-BIB-005")
def test_preferred_team_refuses_a_team_of_another_edition(world: World) -> None:
    other = world.seed.editions["twelve"]
    with world.seed.engine.begin() as connection:
        foreign = new_ulid()
        connection.execute(
            sa.insert(catalog.team).values(id=foreign, edition_id=other, label="elsewhere")
        )
    for team in (foreign, MISSING, "not-an-id"):
        got = world.post(f"/api/v1/editions/{world.edition}/preferred-team", {"team_id": team})
        assert got.status_code in (400, 404, 422), got.text
    assert [t["id"] for t in world.sheet()["teams"]] == world.teams


# -- "Séparer" and the attach: the reserved doors, never by key ---------------------------------
@pytest.mark.req("EF-BIB-019")
def test_the_sheet_shows_a_merge_that_can_still_be_undone_and_not_an_old_one(
    world: World,
) -> None:
    work = world.seed.works["ok"]
    young, old, undone = new_ulid(), new_ulid(), new_ulid()
    with world.seed.engine.begin() as connection:
        for merge_id, created, undone_at in (
            (young, world.env.clock.now(), None),
            (old, world.env.clock.now().replace(year=2024), None),
            (undone, world.env.clock.now(), world.env.clock.now()),
        ):
            connection.execute(
                sa.insert(catalog.merge_log).values(
                    id=merge_id, kind="merge", survivor_id=work, absorbed_id=new_ulid(),
                    snapshot={}, created_at=created, undone_at=undone_at,
                )
            )  # fmt: skip
    shown = world.sheet()
    assert [m["merge_id"] for m in shown["merges"]] == [young]
    assert shown["can"]["separate"] is True
    bad = world.env.post(f"/api/v1/works/merges/{young}/separate", client=world.kid, json={})
    assert bad.status_code == 403
    by_key = world.env.other_client("10.0.0.9").post(
        f"/api/v1/works/merges/{young}/separate", json={},
        headers={"Authorization": f"Bearer {world.key()}"},
    )  # fmt: skip
    assert by_key.status_code in (401, 403)
    assert (
        world.rows("SELECT undone_at FROM merge_log WHERE id=:i", i=young)[0]["undone_at"] is None
    )


def _suggested_attach(world: World) -> tuple[str, str, str]:
    """(holder Work, arriving Edition, attach id): an Edition rated 18+ that shares an identifier
    with a Work rated for everyone is suggested to it, never attached by itself."""
    from shelfhold.catalog.public import (
        AutoAttachService,
        ExternalId,
        IncomingEdition,
        RatingService,
        add_external_id,
    )
    from shelfhold.identity.public import Credential, Principal
    from shelfhold.sdk.age import AgeRating
    from tests.integration.test_schema_catalog import add_edition, add_work
    from tests.null_tombstones import NO_CATALOG_HITS

    holder = world.seed.works["ok"]
    shared = ExternalId("work", "anilist", "30013")
    admin = Principal(
        user_id=new_ulid(), username="a", role="admin", credential=Credential.SESSION,
        session_id=new_ulid(), auth_time=NOW, can_rate=True,
    )  # fmt: skip
    with world.seed.engine.begin() as connection:
        add_external_id(connection, holder, shared, origin="provider")
        work = add_work(connection, title="Dr Stone (uncut)", sort_title="dr stone (uncut)")
        edition = add_edition(connection, work, lang="de")  # a language the holder lacks
        RatingService().rate(connection, holder, AgeRating(0), principal=admin, now=NOW)
        RatingService().rate_edition(connection, edition, AgeRating(18), principal=admin, now=NOW)
        arrival = AutoAttachService(tombstones=NO_CATALOG_HITS).arrive(
            connection, IncomingEdition(edition, ids=(shared,)), now=NOW
        )
    assert arrival.attach_id is not None
    return holder, edition, arrival.attach_id


@pytest.mark.req("EF-BIB-003", "EF-BIB-037")
def test_the_attach_plan_and_its_confirmation_are_reserved_and_the_token_is_the_plans(
    world: World,
) -> None:
    holder, edition, attach_id = _suggested_attach(world)
    shown = world.env.client.get(f"/api/v1/editions/{edition}/sheet").json()
    assert shown["attach"]["attach_id"] == attach_id and shown["can"]["confirm_attach"] is True
    assert world.sheet()["attach"] is None  # the holder's own sheet offers nothing

    unknown = world.post(f"/api/v1/attachments/{MISSING}/plan")
    assert unknown.status_code == 404
    for who in (world.kid, world.member):  # a reserved action: neither a child nor a member
        for path, body in (
            (f"/api/v1/attachments/{attach_id}/plan", {}),
            (f"/api/v1/attachments/{attach_id}/confirm", {"confirm": "x"}),
        ):
            assert world.env.post(path, client=who, json=body).status_code == 403
    for path, body in (
        (f"/api/v1/attachments/{attach_id}/plan", {}),
        (f"/api/v1/attachments/{attach_id}/confirm", {"confirm": "x"}),
    ):  # and never a key
        by_key = world.env.other_client("10.0.0.9").post(
            path, json=body, headers={"Authorization": f"Bearer {world.key()}"}
        )
        assert by_key.status_code in (401, 403)
    assert world.post(f"/api/v1/attachments/{attach_id}/confirm", {}).status_code == 422

    def holder_of() -> str:
        return str(world.rows("SELECT work_id FROM edition WHERE id=:e", e=edition)[0]["work_id"])

    assert holder_of() != holder
    plan = world.post(f"/api/v1/attachments/{attach_id}/plan")
    assert plan.status_code == 200, plan.text
    token = plan.json()["token"]
    assert plan.json()["edition_id"] == edition and plan.json()["work_id"] == holder
    assert holder_of() != holder  # a plan applies nothing
    stale = world.post(f"/api/v1/attachments/{attach_id}/confirm", {"confirm": "0-00000000"})
    assert stale.status_code in (409, 422), stale.text
    assert holder_of() != holder  # not for another token
    done = world.post(f"/api/v1/attachments/{attach_id}/confirm", {"confirm": token})
    assert done.status_code == 200, done.text
    assert holder_of() == holder
    assert world.env.client.get(f"/api/v1/editions/{edition}/sheet").json()["attach"] is None


# -- the filters and the covers: what a child never sees ---------------------------------------
def _series(world: World, title: str, *members: str) -> str:
    series = new_ulid()
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.series).values(id=series, title=title, sort_title=title.lower())
        )
        for position, work in enumerate(members, start=1):
            connection.execute(
                sa.insert(catalog.series_member).values(
                    series_id=series, work_id=work, position=str(position)
                )
            )
    return series


@pytest.mark.req("EF-SRC-039", "EF-USR-006")
def test_series_filters_per_shelf_and_series(world: World) -> None:
    works, seed = world.seed.works, world.seed
    clean = _series(world, "Clean Saga", works["ok"], works["adult_shelf"])
    spoiled = _series(world, "Spoiled Saga", works["ok"], works["above"])  # one hidden member

    def titles(who: TestClient, **query: str) -> list[str]:
        got = who.get("/api/v1/works", params={"limit": 200, **query})
        return sorted(i["title"] for i in got.json()["items"])

    # the admin sees both series and the filter only narrows
    listed = world.env.client.get("/api/v1/works", params={"facets": "true"})
    admin_series = listed.json()["series"]
    assert {s["title"] for s in admin_series} == {"Clean Saga", "Spoiled Saga"}
    assert titles(world.env.client, series_id=spoiled) == [
        "Tale of Harbor Sixteen",
        "Tale of Orchard",
    ]
    assert titles(world.env.client, shelf_id=seed.shelves["family"]) == [
        "Tale of Misfiled Family", "Tale of Orchard",
    ]  # fmt: skip
    assert titles(world.kid, shelf_id=seed.shelves["family"]) == ["Tale of Orchard"]

    # the child: the series with a hidden member is not listed, not filterable, and its title is
    # in no answer; the hidden Work never comes back through a filter
    seen = world.kid.get("/api/v1/works", params={"facets": "true", "limit": 200})
    assert [s["title"] for s in seen.json()["series"]] == ["Clean Saga"]
    assert titles(world.kid, series_id=clean) == ["Tale of Orchard", "Tale of Quiet Shelf"]
    assert titles(world.kid, series_id=spoiled) == []
    assert world.kid.get("/api/v1/works", params={"series_id": spoiled}).json()["total"] == 0
    for marker in ("Spoiled Saga", "Harbor Sixteen", seed.works["above"], seed.editions["above"]):
        for query in ({"facets": "true"}, {"series_id": clean}, {"series_id": spoiled},
                      {"shelf_id": seed.shelves["adult"]}):  # fmt: skip
            assert marker not in world.kid.get("/api/v1/works", params=query).text, (marker, query)
    # a shelf: only the Works she may see that sit on it, and the total agrees with the page
    answer = world.kid.get("/api/v1/works", params={"shelf_id": seed.shelves["adult"]}).json()
    assert sorted(i["title"] for i in answer["items"]) == ["Tale of Mixed Editions",
                                                           "Tale of Orchard",
                                                           "Tale of Quiet Shelf"]  # fmt: skip
    assert answer["total"] == len(answer["items"])
    # a filter with something that is no identifier is refused, not ignored
    assert world.kid.get("/api/v1/works", params={"series_id": "nope"}).status_code == 422
    assert world.kid.get("/api/v1/works", params={"shelf_id": "nope"}).status_code == 422


@pytest.mark.req("EF-BIB-020", "EF-USR-006")
def test_the_library_page_carries_the_thumbnail_address_of_visible_covers_only(
    world: World,
) -> None:
    from tests.thumbnail_env import put_cover

    seed = world.seed
    digests: dict[str, str] = {}
    with seed.engine.begin() as connection:
        for index, key in enumerate(("ok", "above", "adult_edition", "mixed_new")):
            digests[key] = put_cover(
                world.env.settings.data_dir, connection, seed.editions[key], (index * 50, 9, 9)
            )
    mine = world.kid.get("/api/v1/works", params={"limit": 200})
    by_title = {i["title"]: i for i in mine.json()["items"]}
    assert by_title["Tale of Orchard"]["cover_hash"] == digests["ok"]
    for hidden in digests.values():
        if hidden != digests["ok"]:
            assert hidden not in mine.text  # not an address, not a hash
    for marker in (seed.editions["above"], seed.editions["adult_edition"],
                   seed.editions["mixed_new"]):  # fmt: skip
        assert marker not in mine.text
    admin = world.env.client.get("/api/v1/works", params={"limit": 200}).json()["items"]
    assert {i["cover_hash"] for i in admin} >= set(digests.values())
    # the address she was given opens, and only hers do
    assert world.kid.get(f"/api/v1/thumbnails/{digests['ok']}/160").status_code == 200
    assert world.kid.get(f"/api/v1/thumbnails/{digests['above']}/160").status_code == 404
