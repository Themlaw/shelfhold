"""M4 L31 (EF-USR-020, ENF-SEC-010, D22): importable reading lists, through the real application
with real sessions, CSRF and keys.

A list of wanted books is plain text, one title per line (an ISBN if there is one). The import
resolves it against the catalogue only (no source is ever asked), puts what the profile is shown on
a reading shelf, makes a request of the rest, and gives the admin one screen for the whole batch. A
list made for a child is a parental decision (a reserved action); her requests still ask for an age
at approval; her shelf never holds above her ceiling.

The attacks of the card: someone else's batch is an absent one, a child imports nothing, a member
makes nothing for a child, and a key (read-only or not, an admin's included) never acts.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.identity.public import RESERVED_ACTIONS, Effect, local_auth
from shelfhold.infrastructure.schema import catalog
from tests.auth_env import CSRF_HEADER
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    MISSING,
    World,
    api,
    world,
)
from tests.visibility_env import add_profile

LISTS = "/api/v1/request-lists"
COLLECTIONS = "/api/v1/collections"
REQUESTS = "/api/v1/requests"
ISBN = "978-0-306-40615-7"  # the well-known sample number, no real book
UNKNOWN = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
CATALOGUED = "Tale of Orchard"  # a Work the kid is shown
HIDDEN = "Tale of Harbor Sixteen"  # a Work above her ceiling
LIST_TEXT = f"""# a list of books to read
{CATALOGUED}
tale  of QUIET shelf
A Tale Nobody Catalogued
Another Missing Tale ; {ISBN}
"""


@pytest.fixture(autouse=True)
def no_source_is_asked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """An import never searches a source (D22): the port that would is a tripwire."""
    asked: list[str] = []

    def tripwire(*_a: Any, **_k: Any) -> Any:
        asked.append("search")
        raise AssertionError("a list import asked a source")

    monkeypatch.setattr("shelfhold.ops.requests._Ports.search", tripwire)
    return asked


def _post(world: World, client: Any, path: str, body: Any = None) -> Any:
    return client.post(path, json=body, headers={CSRF_HEADER: world.env.csrf(client)})


def _rows(world: World, sql: str, **params: Any) -> list[Any]:
    with world.seed.engine.connect() as connection:
        return list(connection.execute(sa.text(sql), params))


def _requests(world: World) -> list[Any]:
    return _rows(
        world, "SELECT id, requester_user_id, target_kind, target_value, state FROM request"
    )


def _kid_list(world: World, text: str = LIST_TEXT, **extra: Any) -> Any:
    return _post(
        world, world.env.client, f"{LISTS}/for-child/{world.kid_id}", {"text": text, **extra}
    )


def _key_headers(world: World, username: str, **kwargs: Any) -> dict[str, str]:
    key = local_auth(world.seed.engine, world.env.settings).create_api_key(
        name=f"{username}-key", username=username, **kwargs
    )
    return {"Authorization": f"Bearer {key.value}"}


# -- bulk resolution, one screen -------------------------------------------------------------------


@pytest.mark.req("EF-USR-020")
def test_request_from_list_bulk_single_review_screen(world: World) -> None:
    done = _post(world, world.env.client, LISTS, {"text": LIST_TEXT, "name": "Winter"})
    assert done.status_code == 201, done.text
    body = done.json()
    # two lines are in the library (the second one written differently), two are requests
    assert (body["in_library"], body["requested"], body["annotated"]) == (2, 2, 0)
    # the one screen: the whole batch, in the order of the file, one call
    screen = world.env.client.get(f"{LISTS}/{body['id']}")
    assert screen.status_code == 200, screen.text
    shown = screen.json()
    assert shown["name"] == "Winter" and shown["in_library"] == 2 and shown["requested"] == 2
    assert [(i["position"], i["outcome"]) for i in shown["items"]] == [
        (0, "in_library"), (1, "in_library"), (2, "requested"), (3, "requested"),
    ]  # fmt: skip
    assert [i["text"] for i in shown["items"]] == [
        CATALOGUED, "tale  of QUIET shelf".replace("  ", " "), "A Tale Nobody Catalogued",
        "Another Missing Tale",
    ]  # fmt: skip
    assert [i["isbn"] for i in shown["items"]][3] == "9780306406157"
    batches = world.env.client.get(LISTS).json()["items"]
    assert [b["id"] for b in batches] == [body["id"]]
    # a request for each line the library does not hold, none for the others
    asked = _requests(world)
    assert sorted((r.target_kind, r.target_value) for r in asked) == [
        ("free_text", "A Tale Nobody Catalogued"), ("isbn", "9780306406157"),
    ]  # fmt: skip
    assert {r.state for r in asked} == {"pending"}
    # the reading shelf: the admin's own list, in the order of the file
    shelf = world.env.client.get(f"{COLLECTIONS}/{shown['collection_id']}").json()
    assert [i["title"] for i in shelf["items"]] == [CATALOGUED, "Tale of Quiet Shelf"]
    assert shelf["name"] == "Winter" and shelf["scope"] == "profile"


@pytest.mark.req("EF-USR-020")
def test_a_list_is_read_as_untrusted_text_and_bounded(world: World) -> None:
    refused = {
        "too_large": "x" * 70_000,
        "too_many_lines": "\n".join(f"Title {i}" for i in range(101)),
        "line_too_long": "t" * 301,
        "not_text": "a title\x00with a null",
        "empty": "# only a comment\n\n",
    }
    for reason, text in refused.items():
        answer = _post(world, world.env.client, LISTS, {"text": text})
        assert answer.status_code == 422, (reason, answer.text)
        if reason != "too_large":  # the body bound of the schema answers before the parser
            assert answer.json()["code"] == "request.invalid_list", reason
            assert answer.json()["reason"] == reason
        assert text[:20] not in answer.text or reason == "empty"  # never a piece of the list
    assert _requests(world) == [] and _rows(world, "SELECT 1 FROM request_list_import") == []
    # what looks like a path, an address, a tag or a command is only a title: never followed
    odd = "\n".join(["../../etc/passwd", "https://example.invalid/x", "<b>bold</b>", "$(id)"])
    done = _post(world, world.env.client, LISTS, {"text": odd})
    assert done.status_code == 201 and done.json()["requested"] == 4
    assert sorted(r.target_value for r in _requests(world)) == sorted(odd.splitlines())
    # a 100 lines list is the limit, and is read whole
    full = "\n".join(f"Missing {i}" for i in range(100))
    assert _post(world, world.env.client, LISTS, {"text": full}).status_code == 201
    # one more list would pile more than 200 open requests on the same profile: refused whole
    assert _post(world, world.env.client, LISTS, {"text": "Last Missing"}).status_code == 201
    before = len(_requests(world))
    over = _post(
        world, world.env.client, LISTS, {"text": "\n".join(f"Over {i}" for i in range(60))}
    )
    assert over.status_code == 429 and over.json()["code"] == "request.too_many_pending"
    assert len(_requests(world)) == before


# -- a list for a child ----------------------------------------------------------------------------


@pytest.mark.req("EF-USR-020", "EF-USR-006")
def test_list_for_child_attaches_reading_shelf_to_profile(world: World) -> None:
    text = f"{CATALOGUED}\n{HIDDEN}\nA Tale Nobody Catalogued\n"
    done = _kid_list(world, text, name="Kid list")
    assert done.status_code == 201, done.text
    body = done.json()
    assert (body["in_library"], body["requested"]) == (
        1,
        2,
    )  # a Work above her ceiling is a request
    # the shelf is hers: owned by her profile, read by her, never above her ceiling
    owners = _rows(
        world, "SELECT owner_id, name FROM collection WHERE id = :i", i=body["collection_id"]
    )
    assert owners == [(world.kid_id, "Kid list")]
    items = _rows(
        world,
        "SELECT work_id FROM collection_item WHERE collection_id = :i",
        i=body["collection_id"],
    )
    assert [r[0] for r in items] == [world.seed.works["ok"]]  # the hidden Work is not on it at all
    mine = world.kid.get(f"{COLLECTIONS}/{body['collection_id']}")
    assert mine.status_code == 200 and mine.json()["mine"] is True
    assert [i["title"] for i in mine.json()["items"]] == [CATALOGUED]
    world.clean(mine.text)
    listed = world.kid.get(COLLECTIONS)
    assert [c["id"] for c in listed.json()["items"]] == [body["collection_id"]]
    world.clean(listed.text)
    # the requests are hers, and she reads them as she reads any of hers
    asked = _requests(world)
    assert {r.requester_user_id for r in asked} == {world.kid_id} and len(asked) == 2
    hers = world.kid.get(REQUESTS)
    assert hers.status_code == 200 and len(hers.json()["items"]) == 2
    world.clean(hers.text)
    # another profile reads nothing of it: an absent shelf, an absent batch
    other = add_profile(world.env, "ann", "member", "10.0.0.11")
    assert other.get(f"{COLLECTIONS}/{body['collection_id']}").status_code == 404
    assert other.get(f"{LISTS}/{body['id']}").status_code == 404
    # no source was asked for her, ever (the tripwire of the fixture)


@pytest.mark.req("EF-USR-020", "EF-USR-010")
def test_child_list_items_still_require_age_at_approval(world: World) -> None:
    done = _kid_list(world, f"{HIDDEN}\nA Tale Nobody Catalogued\n")
    assert done.status_code == 201, done.text
    screen = world.env.client.get(f"{LISTS}/{done.json()['id']}").json()
    assert [i["needs_age"] for i in screen["items"]] == [True, True]
    for item in screen["items"]:
        rid = item["request_id"]
        preview = world.env.client.get(f"{REQUESTS}/{rid}/approval").json()
        assert preview["needs_age"] is True
    # the Work that exists is approved only with an age; without one, nothing changes
    rid = screen["items"][0]["request_id"]
    refused = _post(world, world.env.client, f"{REQUESTS}/{rid}/approve", {})
    assert refused.status_code == 422 and refused.json()["code"] == "request.choose_age"
    assert {r.state for r in _requests(world)} == {"pending"}
    # and a text the library cannot resolve is never searched for her (D22)
    other = screen["items"][1]["request_id"]
    proposals = _post(world, world.env.client, f"{REQUESTS}/{other}/proposals")
    assert proposals.status_code == 403 and proposals.json()["code"] == "request.not_allowed"


@pytest.mark.req("EF-USR-020")
def test_annotated_edition_warning(world: World) -> None:
    text = "\n".join(
        [
            "Tale of Orchard (annotated edition)",
            "Édition critique de A Missing Tale",
            "A Commented Missing Tale",
            "Plain Missing Tale",
            "Annotated Missing Tale ; " + ISBN,
        ]
    )
    done = _post(world, world.env.client, LISTS, {"text": text})
    assert done.status_code == 201, done.text
    assert done.json()["annotated"] == 4  # a warning, never a refusal: every line was read
    items = world.env.client.get(f"{LISTS}/{done.json()['id']}").json()["items"]
    assert [i["annotated"] for i in items] == [True, True, True, False, True]
    assert len(items) == 5 and done.json()["in_library"] + done.json()["requested"] == 5


@pytest.mark.req("EF-USR-020", "ENF-SEC-010", "EF-USR-024")
def test_reading_shelf_for_child_created_as_parental_reserved_action(world: World) -> None:
    action = RESERVED_ACTIONS["reading_shelf.attach"]
    assert action.name == "reading_shelf.attach" and Effect.PARENTAL_CONTROL in action.effects
    path = f"{LISTS}/for-child/{world.kid_id}"
    body = {"text": CATALOGUED}
    nothing = lambda: _rows(world, "SELECT 1 FROM request_list_import") == []  # noqa: E731
    # a member and a child are refused; a stranger to the instance is not let in
    ann = add_profile(world.env, "ann", "member", "10.0.0.11")
    for client in (ann, world.kid):
        refused = _post(world, client, path, body)
        assert refused.status_code == 403 and refused.json()["code"] == "forbidden"
    assert world.env.other_client("10.0.0.9").post(path, json=body).status_code in {401, 403}
    # a key never acts, whatever its scope and whoever holds it
    for flags in ({"read_only": True}, {}):
        headers = _key_headers(world, "admin", **flags)
        answer = world.env.other_client("10.0.0.9").post(path, json=body, headers=headers)
        assert answer.status_code in {401, 403}, flags
        assert answer.json()["code"] in {"credential_not_allowed", "forbidden"}
    assert nothing() and _requests(world) == []
    # a session that is not fresh is asked to log in again (a restricted profile exists)
    world.env.clock.advance(timedelta(minutes=20))
    stale = world.env.client.post(path, json=body, headers={CSRF_HEADER: world.env.csrf()})
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert nothing()
    # a fresh admin session is let through, and the decision leaves its two lines
    assert world.env.login().status_code == 200
    done = _post(world, world.env.client, path, body)
    assert done.status_code == 201, done.text
    actions = [r[0] for r in _rows(world, "SELECT action FROM audit")]
    assert "reading_shelf.attach" in actions  # the guard's line
    assert "reading_shelf.attached" in actions  # the service's, in the transaction of the change
    # the shelf is hers and not the admin's
    assert _rows(world, "SELECT owner_id FROM collection") == [(world.kid_id,)]


# -- the attacks -----------------------------------------------------------------------------------


@pytest.mark.req("EF-USR-020", "ENF-SEC-010")
def test_a_child_imports_nothing_for_herself_or_anyone(world: World) -> None:
    for path in (LISTS, f"{LISTS}/for-child/{world.kid_id}", f"{LISTS}/for-child/{UNKNOWN}"):
        refused = _post(world, world.kid, path, {"text": CATALOGUED})
        assert refused.status_code == 403, (path, refused.text)
        assert refused.json()["code"] in {"request.kind_not_allowed", "forbidden"}
    assert _requests(world) == [] and _rows(world, "SELECT 1 FROM collection") == []
    # she reads no batch: an admin's, nor a guessed one
    mine = _post(world, world.env.client, LISTS, {"text": CATALOGUED})
    for path in (f"{LISTS}/{mine.json()['id']}", f"{LISTS}/{UNKNOWN}", LISTS):
        assert world.kid.get(path).status_code in {403, 404}, path
    assert (
        world.kid.get(f"{LISTS}/{mine.json()['id']}").text
        == world.kid.get(f"{LISTS}/{UNKNOWN}").text
    )


@pytest.mark.req("EF-USR-020", "ENF-SEC-010")
def test_a_member_list_is_private_and_a_batch_of_someone_else_is_absent(world: World) -> None:
    ann = add_profile(world.env, "ann", "member", "10.0.0.11")
    bob = add_profile(world.env, "bob", "member", "10.0.0.12")
    made = _post(world, ann, LISTS, {"text": "Ann Secret Title\n" + CATALOGUED})
    assert made.status_code == 201, made.text
    path = f"{LISTS}/{made.json()['id']}"
    assert ann.get(path).status_code == 200 and ann.get(path).json()["items"][0]["text"]
    # another member, a child, a guessed identifier: the same answer as for one nobody ever had
    absent = bob.get(f"{LISTS}/{UNKNOWN}")
    assert absent.status_code == 404
    for stranger in (bob, world.kid):
        answer = stranger.get(path)
        assert answer.status_code in {403, 404}
        if answer.status_code == 404:
            assert answer.json()["type"] == absent.json()["type"]
    assert bob.get(LISTS).json()["items"] == []
    # the admin sees the batch exists, never what an adult asked for (as the queue hides it)
    seen = world.env.client.get(path)
    assert seen.status_code == 200
    assert all(i["text"] is None and i["isbn"] is None for i in seen.json()["items"])
    assert "Ann Secret Title" not in seen.text
    assert [b["id"] for b in world.env.client.get(LISTS).json()["items"]] == []
    # a member makes no list for anyone else, a child's included, and none for an absent profile
    assert _post(world, ann, f"{LISTS}/for-child/{world.kid_id}", {"text": "x"}).status_code == 403
    # an admin makes none for an adult: nobody files another adult's requests
    adult = _post(
        world, world.env.client, f"{LISTS}/for-child/{_user(world, 'ann')}", {"text": "x"}
    )
    assert adult.status_code == 403 and adult.json()["code"] == "request.not_allowed"
    absent_profile = _post(world, world.env.client, f"{LISTS}/for-child/{UNKNOWN}", {"text": "x"})
    assert (
        absent_profile.status_code == 404 and absent_profile.json()["code"] == "request.not_found"
    )
    assert len(_requests(world)) == 1  # Ann's one missing title, and nothing else


def _user(world: World, name: str) -> str:
    return str(_rows(world, "SELECT id FROM user WHERE username = :n", n=name)[0][0])


@pytest.mark.req("EF-USR-020", "ENF-SEC-010", "EF-USR-024")
def test_a_key_never_imports_or_reads_a_list(world: World) -> None:
    made = _post(world, world.env.client, LISTS, {"text": CATALOGUED})
    for username in ("admin", "kid"):
        for flags in ({"read_only": True}, {}, {"scope": "request"}):
            headers = _key_headers(world, username, **flags)
            other = world.env.other_client("10.0.0.9")
            for method, path in (
                ("POST", LISTS), ("GET", LISTS), ("GET", f"{LISTS}/{made.json()['id']}"),
                ("POST", f"{LISTS}/for-child/{world.kid_id}"),
            ):  # fmt: skip
                answer = (
                    other.post(path, json={"text": "A Tale From A Key"}, headers=headers)
                    if method == "POST"
                    else other.get(path, headers=headers)
                )
                assert answer.status_code in {401, 403}, (username, flags, method, path)
    assert [r.target_value for r in _requests(world)] == []
    assert _rows(world, "SELECT COUNT(*) FROM request_list_import") == [(1,)]


@pytest.mark.req("EF-USR-020", "ENF-SEC-010")
def test_importing_twice_makes_one_request_per_title(world: World) -> None:
    for _ in range(2):
        assert (
            _post(world, world.env.client, LISTS, {"text": "Missing One\nmissing  one"}).status_code
            == 201
        )
    assert [r.target_value for r in _requests(world)] == ["Missing One"]
    assert len(world.env.client.get(LISTS).json()["items"]) == 2
    assert MISSING  # the shared absent identifier of the child-surface suite


@pytest.mark.req("EF-USR-020")
def test_a_removed_work_leaves_the_batch_readable(world: World) -> None:
    done = _post(world, world.env.client, LISTS, {"text": CATALOGUED})
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.delete(catalog.work).where(catalog.work.c.id == world.seed.works["ok"])
        )
    shown = world.env.client.get(f"{LISTS}/{done.json()['id']}")
    assert shown.status_code == 200 and shown.json()["items"][0]["outcome"] == "in_library"
