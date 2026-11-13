# ruff: noqa: F811  (the `world` fixture of the child suite is imported, then used as an argument)
"""M3 L41 (EF-UX-010, EF-UX-005, EF-UX-008, EF-USR-016, ENF-SEC-010): what the home page and the
activity page read from the server.

The family reads one state per request (En préparation, Disponible dans Kavita, En attente) and
a link that opens Kavita; the steps and `koreader_reset` are the admin's. The state and the link
are decided here, by the server, never by the screen. "Arrivé récemment" is the reader's own view
of the Works (the one `ViewerScope` filter), newest first: a child sees no hidden Work, not by a
title, a count, an empty page or a page number. "Mes demandes" holds the caller's own requests only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity, platform
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    VISIBLE_FOR_CEILING_10,
    World,
    api,
    world,
)
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-UX-010")

ACTIVITY = "/api/v1/activity"
REQUESTS = "/api/v1/requests"
WORKS = "/api/v1/works"
KAVITA = "http://kavita.test:5000"
WHEN = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
STEPS = {"requested", "downloaded", "fetched", "preparing"}


def add_kavita(world: World) -> None:
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.insert(export.export_target).values(
                id=new_ulid(), kind="kavita", name="family", config={"url": KAVITA}
            )
        )


def add_request(world: World, owner: str, state: str, *, visibility: str = "household") -> str:
    request_id = new_ulid()
    work_id = world.seed.works["ok"]
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.insert(identity.request).values(
                id=request_id, requester_user_id=owner, target_kind="work_id",
                target_value=work_id, state=state, visibility=visibility,
                work_id=work_id if state == "arrived" else None,
            )
        )  # fmt: skip
    return request_id


def publish(world: World, event_type: str, payload: dict[str, object], **row: object) -> None:
    values: dict[str, object] = {
        "type": event_type, "schema_version": 1, "payload": payload, "occurred_at": WHEN,
        "actor_id": world.kid_id, "visibility": "household", **row,
    }  # fmt: skip
    with world.seed.engine.begin() as connection:
        connection.execute(sa.insert(platform.event).values(**values))


def member_client(world: World) -> TestClient:
    return add_profile(world.env, "mum", "member", "10.0.0.3")


def mine(client: TestClient) -> dict[str, dict[str, object]]:
    answer = client.get(REQUESTS)
    assert answer.status_code == 200
    return {item["id"]: item for item in answer.json()["items"]}


# -- one state for the family, and the link that opens Kavita ----------------------------------


@pytest.mark.req("EF-UX-010")
def test_activity_family_single_state_with_open_in_kavita(world: World) -> None:
    add_kavita(world)
    waiting = add_request(world, world.kid_id, "pending")
    preparing = add_request(world, world.kid_id, "approved")
    done = add_request(world, world.kid_id, "arrived")
    shown = mine(world.kid)
    assert {
        k: (shown[k]["family_state"], shown[k]["open_url"]) for k in (waiting, preparing, done)
    } == {
        waiting: ("waiting", None),
        preparing: ("preparing", None),
        done: ("available", KAVITA),
    }
    # the member's own "Mes demandes" says the same, in the same words
    assert mine(member_client(world)) == {}
    # the steps are not the family's: no raw state is a second word for the same thing
    assert STEPS.isdisjoint({str(item["family_state"]) for item in shown.values()})


@pytest.mark.req("EF-UX-010")
def test_family_state_has_no_link_without_a_kavita_target(world: World) -> None:
    done = add_request(world, world.kid_id, "arrived")
    assert mine(world.kid)[done]["open_url"] is None  # no link is invented


@pytest.mark.req("EF-UX-010")
def test_activity_arrival_is_one_state_with_the_link_for_a_child(world: World) -> None:
    add_kavita(world)
    publish(world, "ops.request_arrived", {"request_id": new_ulid()})
    [item] = world.kid.get(ACTIVITY).json()
    assert (item["family_state"], item["open_url"]) == ("available", KAVITA)
    assert "koreader_reset" not in item["payload"]


@pytest.mark.req("EF-UX-010")
def test_family_never_reads_a_step_in_the_activity_payload(world: World) -> None:
    """A request of the activity feed carries its raw step only for the admin."""
    from shelfhold.events.activity import download_request_event
    from shelfhold.interfaces.http_api.activity import family_payload

    event = download_request_event(
        request_id=new_ulid(), state="downloaded", visibility="household", requester_id=None,
        requester_role=None, title=None,
    )  # fmt: skip
    assert family_payload("child", event.type, event.payload) == {}
    assert family_payload("member", event.type, event.payload) == {}
    assert family_payload("admin", event.type, event.payload) == {"state": "downloaded"}


# -- koreader_reset is the admin's ------------------------------------------------------------


@pytest.mark.req("EF-UX-010")
def test_activity_shows_koreader_reset(world: World) -> None:
    work_id = world.seed.works["ok"]
    payload = {"work_id": work_id, "unit_id": new_ulid(), "reason": "better_version",
               "koreader_reset": True}  # fmt: skip
    publish(world, "unit.upgraded", payload, visibility="ops", actor_id=None)
    admin = [i for i in world.env.client.get(ACTIVITY).json() if i["type"] == "unit.upgraded"]
    assert [i["koreader_reset"] for i in admin] == [True]
    assert admin[0]["payload"]["koreader_reset"] is True  # the step, for the admin


@pytest.mark.req("EF-UX-010")
def test_koreader_reset_never_reaches_the_family(world: World) -> None:
    work_id = world.seed.works["ok"]
    body = {"work_id": work_id, "reason": "better_version", "koreader_reset": True}
    publish(world, "unit.upgraded", body, visibility="ops", actor_id=None)  # an operation
    publish(world, "unit.upgraded", body)  # even a household one: the flag is the admin's
    for client in (world.kid, member_client(world)):
        for item in client.get(ACTIVITY).json():
            assert item["koreader_reset"] is False
            assert "koreader_reset" not in item["payload"]
    assert "koreader_reset" not in str(world.kid.get(REQUESTS).json())


# -- Arrivé récemment: the reader's own view, newest first ------------------------------------


def _recent(client: TestClient, **params: object) -> list[str]:
    answer = client.get(WORKS, params={"sort": "recent", **params})
    assert answer.status_code == 200
    return [i["work_id"] for i in answer.json()["items"]]


@pytest.mark.req("EF-UX-005")
def test_home_recently_arrived_respects_child_filter(world: World) -> None:
    answer = world.kid.get(WORKS, params={"sort": "recent", "limit": 200})
    assert answer.status_code == 200
    world.clean(answer.text)
    visible = {world.seed.works[k] for k in VISIBLE_FOR_CEILING_10}
    assert {i["work_id"] for i in answer.json()["items"]} == visible
    assert answer.json()["total"] == len(visible)  # the count is hers too


@pytest.mark.req("EF-UX-005")
def test_recent_is_newest_first_for_the_admin(world: World) -> None:
    ids = _recent(world.env.client, limit=200)
    with world.seed.engine.connect() as connection:
        seq = {
            r.id: r.created_seq
            for r in connection.execute(sa.text("SELECT id, created_seq FROM work"))
        }
        order = [
            r.id for r in connection.execute(sa.text("SELECT id FROM work ORDER BY rowid DESC"))
        ]
    assert sorted(ids) == sorted(order)
    assert [seq[i] for i in ids] == sorted((seq[i] for i in ids), reverse=True)


@pytest.mark.req("EF-UX-005")
def test_recent_pages_never_carry_a_hidden_work_for_a_child(world: World) -> None:
    """Attack: a page, an offset past the end, a limit of one, a bad sort: nothing hidden."""
    visible = {world.seed.works[k] for k in VISIBLE_FOR_CEILING_10}
    seen: list[str] = []
    for offset in range(0, 6):
        page = world.kid.get(WORKS, params={"sort": "recent", "limit": 1, "offset": offset})
        assert page.status_code == 200
        world.clean(page.text)
        seen += [i["work_id"] for i in page.json()["items"]]
    assert set(seen) == visible and len(seen) == len(visible)
    assert _recent(world.kid, limit=5, offset=50) == []
    assert world.kid.get(WORKS, params={"sort": "created_at DESC"}).status_code == 422


@pytest.mark.req("EF-UX-005")
def test_recent_for_a_child_with_nothing_visible_is_an_empty_page_not_a_clue(
    world: World,
) -> None:
    """A child whose ceiling hides everything gets the empty page of an empty library."""
    low = add_profile(world.env, "tiny", "child", "10.0.0.4", max_age_rating=1)
    answer = low.get(WORKS, params={"sort": "recent"})
    assert answer.status_code == 200
    body = answer.json()
    assert (body["items"], body["total"], body["removed"]) == ([], 0, [])
    world.clean(answer.text)


# -- the activity feed gives nothing of another profile beyond the card -------------------------


@pytest.mark.req("EF-USR-016")
def test_activity_of_another_profile_stays_private(world: World) -> None:
    mum = member_client(world)
    other = add_profile(world.env, "sister", "child", "10.0.0.5", max_age_rating=10)
    publish(world, "ops.request_arrived", {"request_id": new_ulid()}, visibility="private")
    assert [i["type"] for i in world.kid.get(ACTIVITY).json()] == ["ops.request_arrived"]
    assert other.get(ACTIVITY).json() == []  # a sibling's request is not hers to read
    assert mum.get(ACTIVITY).json() == []  # nor a parent's, for a private one (the admin masks)
    masked = [
        i for i in world.env.client.get(ACTIVITY).json() if i["type"] == "ops.request_arrived"
    ]
    assert all(i["title"] is None for i in masked)


@pytest.mark.req("EF-USR-016")
def test_my_requests_are_only_mine(world: World) -> None:
    mum = member_client(world)
    add_kavita(world)
    own = add_request(world, world.kid_id, "arrived")
    assert set(mine(world.kid)) == {own}
    assert mine(mum) == {}  # another profile's request is not in the list
    assert mum.get(f"{REQUESTS}/{own}").status_code == 404
    assert world.kid.get(f"{REQUESTS}/{new_ulid()}").status_code == 404


@pytest.mark.req("ENF-SEC-010")
def test_open_url_is_never_set_by_the_client(world: World) -> None:
    """The link is read from the Kavita target on the server: nothing the client sends moves it."""
    add_kavita(world)
    done = add_request(world, world.kid_id, "arrived")
    answer = world.kid.get(f"{REQUESTS}/{done}", params={"open_url": "http://evil.example"})
    assert answer.json()["open_url"] == KAVITA
