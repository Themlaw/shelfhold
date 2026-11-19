"""M5 L21 (EF-BIB-017, EF-USR-006, ENF-SEC-010): saved selections, read as a profile.

A saved selection is a stored query in a closed grammar (reading state, kind, shelf, language,
recent addition). It holds no Work and no result: it is evaluated each time it is read, in the scope
of the profile that reads it, so a child never sees a hidden Work through it, nor in its count. It
belongs to one profile and is read by that profile alone (not by an admin, not by a key of another
profile). Real application, real sessions, real keys.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, reading
from tests.auth_env import CSRF_HEADER
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    World,
    api,
    world,
)
from tests.visibility_env import _edition, _work, add_profile

pytestmark = pytest.mark.req("EF-BIB-017")

SELECTIONS = "/api/v1/selections"
HASH = "sha256:" + "ab" * 32
ABSENT = "01HZZZZZZZZZZZZZZZZZZZZZZM"


def _post(world: World, client: Any, path: str, body: Any = None) -> Any:
    return client.post(path, json=body, headers={CSRF_HEADER: world.env.csrf(client)})


def _put(world: World, client: Any, path: str, body: Any = None) -> Any:
    return client.put(path, json=body, headers={CSRF_HEADER: world.env.csrf(client)})


def _delete(world: World, client: Any, path: str) -> Any:
    return client.delete(path, headers={CSRF_HEADER: world.env.csrf(client)})


def _make(world: World, client: Any, name: str, query: dict[str, Any]) -> str:
    made = _post(world, client, SELECTIONS, {"name": name, "query": query})
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def _kind(world: World) -> str:
    with world.seed.engine.connect() as c:
        return str(
            c.execute(
                sa.select(catalog.work.c.kind).where(catalog.work.c.id == world.seed.works["ok"])
            ).scalar_one()
        )


def _titles(answer: Any) -> list[str]:
    return sorted(item["title"] for item in answer.json()["items"])


def _titles_of(world: World, keys: set[str]) -> list[str]:
    return sorted(world.seed.title_of(k) for k in keys)


def _member(world: World, name: str = "mum", ip: str = "10.0.0.3") -> Any:
    return add_profile(world.env, name, "member", ip)


def _shape(response: Any) -> tuple[Any, ...]:
    body = response.json()
    return response.status_code, body.get("type"), body.get("title"), body.get("detail")


def _rows(world: World) -> list[Any]:
    with world.seed.engine.connect() as c:
        return list(c.execute(sa.text("SELECT * FROM saved_selection ORDER BY id")).all())


def _user_id(world: World, username: str) -> str:
    with world.seed.engine.connect() as c:
        return str(
            c.execute(sa.text("SELECT id FROM user WHERE username = :u"), {"u": username}).scalar()
        )


# -- the selections of a profile ----------------------------------------------------------------


def test_saved_selections(world: World) -> None:
    admin = world.env.client
    made = _make(world, admin, "In French", {"lang": ["fr"]})
    listed = admin.get(SELECTIONS)
    assert listed.status_code == 200
    assert [(i["id"], i["name"], i["query"]) for i in listed.json()["items"]] == [
        (made, "In French", {"lang": ["fr"]})
    ]
    detail = admin.get(f"{SELECTIONS}/{made}")
    assert detail.status_code == 200
    body = detail.json()
    assert _titles(detail) == _titles_of(world, {"mixed"})  # one Work has an Edition in French
    assert (body["total"], body["capped"], body["valid"]) == (1, False, True)
    # changed, then removed
    renamed = _put(
        world, admin, f"{SELECTIONS}/{made}", {"name": "Fr+En", "query": {"lang": ["fr", "en"]}}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Fr+En" and renamed.json()["query"] == {"lang": ["en", "fr"]}
    again = admin.get(f"{SELECTIONS}/{made}")
    assert again.json()["total"] == len(again.json()["items"]) > 1
    assert _delete(world, admin, f"{SELECTIONS}/{made}").status_code == 204
    assert admin.get(f"{SELECTIONS}/{made}").status_code == 404
    assert admin.get(SELECTIONS).json()["items"] == []
    assert _delete(world, admin, f"{SELECTIONS}/{made}").status_code == 404


def test_every_criterion_selects_what_it_says(world: World) -> None:
    admin, works = world.env.client, world.seed.works

    def found(query: dict[str, Any]) -> list[str]:
        made = _make(world, admin, "q", query)
        answer = admin.get(f"{SELECTIONS}/{made}")
        assert answer.status_code == 200, answer.text
        return _titles(answer)

    assert found({"kind": [_kind(world)]}) == _titles_of(world, set(works))
    assert found({"kind": ["no-such-kind"]}) == []
    shelf_family = world.seed.shelves["family"]
    assert found({"shelf": [shelf_family]}) == _titles_of(world, {"misfiled", "reserved"})
    assert found({"shelf": [new_ulid()]}) == []  # a shelf that does not exist selects nothing
    assert found({"lang": ["en"]}) != found({"lang": ["fr"]})
    # recent: only a Work added within the number of days
    old = datetime.now(UTC) - timedelta(days=100)
    with world.seed.engine.begin() as c:
        c.execute(
            sa.update(catalog.work).where(catalog.work.c.id == works["ok"]).values(created_at=old)
        )
    recent = found({"added_within_days": 30})
    assert world.seed.title_of("ok") not in recent and world.seed.title_of("mixed") in recent
    assert world.seed.title_of("ok") in found({"added_within_days": 365})
    # the criteria add up (and), the values of one criterion are alternatives (or)
    assert found({"kind": [_kind(world)], "lang": ["fr"]}) == _titles_of(world, {"mixed"})
    assert found({"lang": ["fr", "en"], "shelf": [shelf_family]}) == _titles_of(
        world, {"misfiled", "reserved"}
    )


def _seed_reading(world: World, reader: str) -> None:
    """For `reader`: `ok` half read (one of two Units), `adult_shelf` read, `above` read (hidden for
    the child), `mixed` never opened."""
    reader_id = _user_id(world, reader)
    with world.seed.engine.begin() as c:
        units: dict[str, list[str]] = {}
        for key, count in (("ok", 2), ("adult_shelf", 1), ("above", 1)):
            edition = world.seed.editions[key]
            units[key] = []
            for number in range(count):
                unit_id = new_ulid()
                units[key].append(unit_id)
                c.execute(
                    sa.insert(catalog.unit).values(
                        id=unit_id, edition_id=edition, number_raw=str(number + 1),
                        number_norm=str(number + 1),
                    )
                )  # fmt: skip
        for read in (units["ok"][:1], units["adult_shelf"], units["above"]):
            c.execute(
                sa.insert(reading.reading_position).values(
                    id=new_ulid(), user_id=reader_id, carrier_type="content",
                    carrier_id=new_ulid(), content_hash=HASH, locator={},
                    client_ts=datetime.now(UTC), read_units=read,
                )
            )  # fmt: skip


def test_reading_state_is_the_one_of_the_profile_that_reads(world: World) -> None:
    _seed_reading(world, "kid")
    admin, kid = world.env.client, world.kid

    def kid_sees(states: list[str]) -> list[str]:
        made = _make(world, kid, "mine", {"reading_state": states})
        answer = kid.get(f"{SELECTIONS}/{made}")
        world.clean(answer.text)
        return _titles(answer)

    assert kid_sees(["unread"]) == _titles_of(world, {"mixed"})
    assert kid_sees(["in_progress"]) == _titles_of(world, {"ok"})
    assert kid_sees(["read"]) == _titles_of(world, {"adult_shelf"})  # not `above`: hidden
    assert kid_sees(["unread", "read"]) == _titles_of(world, {"mixed", "adult_shelf"})
    # the same state read by someone who has not read anything: everything is unread to her
    made = _make(world, admin, "mine", {"reading_state": ["unread"]})
    assert admin.get(f"{SELECTIONS}/{made}").json()["total"] == len(world.seed.works)


def test_selection_evaluated_in_reader_profile_scope(world: World) -> None:
    works, kid, admin = world.seed.works, world.kid, world.env.client
    query = {"kind": [_kind(world)]}
    mine = _make(world, kid, "Everything", query)
    adults = _make(world, admin, "Everything", query)
    seen = kid.get(f"{SELECTIONS}/{mine}")
    assert seen.status_code == 200
    assert _titles(seen) == _titles_of(world, {"ok", "adult_shelf", "mixed"})
    assert seen.json()["total"] == 3  # the count is made of what she may see, nothing else
    world.clean(seen.text)
    assert admin.get(f"{SELECTIONS}/{adults}").json()["total"] == len(works)
    # what is stored is the query, never a result
    stored = json.dumps([tuple(row) for row in _rows(world)], default=str)
    assert not [w for w in works.values() if w in stored]
    assert not [t for t in (world.seed.title_of(k) for k in works) if t in stored]
    # evaluated at each read: a Work that arrives after is there, one re-rated above her is gone
    with world.seed.engine.begin() as c:
        new = _work(c, _kind(world), "Tale of a Late Arrival", age=6)
        _edition(c, new, shelf_id=world.seed.shelves["adult"])
        c.execute(
            sa.update(catalog.work).where(catalog.work.c.id == works["ok"]).values(age_rating=16)
        )
    after = kid.get(f"{SELECTIONS}/{mine}")
    assert _titles(after) == sorted(
        [*_titles_of(world, {"adult_shelf", "mixed"}), "Tale of a Late Arrival"]
    )
    assert after.json()["total"] == 3
    world.clean(after.text)
    # a shelf reserved to another profile selects nothing for her, and says nothing of why
    closed = _make(world, kid, "Closed", {"shelf": [world.seed.shelves["family"]]})
    nothing = kid.get(f"{SELECTIONS}/{closed}")
    assert nothing.status_code == 200
    assert (nothing.json()["items"], nothing.json()["total"]) == ([], 0)
    world.clean(nothing.text)
    # the page and the count agree on every page
    paged = kid.get(f"{SELECTIONS}/{mine}", params={"limit": 1, "offset": 1})
    assert len(paged.json()["items"]) == 1 and paged.json()["total"] == 3


def test_a_child_cannot_name_what_she_may_not_see(world: World) -> None:
    """Whatever the query says (a hidden kind of Work, a closed shelf, a state of a hidden Work),
    the answer is a list of what she may see and a count of the same."""
    kid, works = world.kid, world.seed.works
    for query in (
        {"shelf": [world.seed.shelves["family"], world.seed.shelves["adult"]]},
        {"lang": ["fr"]},  # the Edition of `mixed` in French is one she may not see
        {"kind": [_kind(world)], "reading_state": ["unread", "in_progress", "read"]},
    ):
        made = _make(world, kid, "q", query)
        answer = kid.get(f"{SELECTIONS}/{made}")
        assert answer.status_code == 200
        world.clean(answer.text)
        assert answer.json()["total"] == len(answer.json()["items"])
        assert not {i["work_id"] for i in answer.json()["items"]} & {
            works[k] for k in ("above", "x", "flagged", "unrated", "orphan", "twelve")
        }


# -- the grammar is closed ---------------------------------------------------------------------


def test_selection_query_grammar_closed(world: World) -> None:
    admin = world.env.client
    attacks: list[Any] = [
        {"kind": ["manga"], "sql": "1=1"},
        {"kind": ["manga' OR '1'='1"]},
        {"kind": [["manga"]]},
        {"kind": {"$ne": "manga"}},
        {"reading_state": ["archived"]},
        {"shelf": ["not-an-id"]},
        {"added_within_days": "30; DROP TABLE work"},
        {"added_within_days": 100000},
        {"lang": ["fr"] * 3 + ["x" * 100]},
        {"kind": [f"k{i}" for i in range(500)]},
        {},
        [],
        "kind = manga",
        None,
        {"query": {"kind": ["manga"]}},
    ]
    for query in attacks:
        refused = _post(world, admin, SELECTIONS, {"name": "bad", "query": query})
        assert refused.status_code == 422, (query, refused.status_code)
        assert _rows(world) == [], query  # nothing was stored
    deep = (
        '{"kind":[' * 3000 + '"a"' + "]}" * 3000
    )  # written by hand: json.dumps would not go so deep
    sent = admin.post(
        SELECTIONS, content='{"name":"deep","query":' + deep + "}",
        headers={CSRF_HEADER: world.env.csrf(admin), "Content-Type": "application/json"},
    )  # fmt: skip
    assert 400 <= sent.status_code < 500 and _rows(world) == []
    huge = admin.post(
        SELECTIONS, content=json.dumps({"name": "huge", "query": {"kind": ["a"] * 200_000}}),
        headers={CSRF_HEADER: world.env.csrf(admin), "Content-Type": "application/json"},
    )  # fmt: skip
    assert 400 <= huge.status_code < 500 and _rows(world) == []
    for name in ("", " ", "x" * 201):
        assert (
            _post(world, admin, SELECTIONS, {"name": name, "query": {"kind": ["a"]}}).status_code
            == 422
        )
    # an update is held to the same grammar
    made = _make(world, admin, "ok", {"kind": ["manga"]})
    bad = _put(world, admin, f"{SELECTIONS}/{made}", {"query": {"sql": "1"}})
    assert bad.status_code == 422
    assert admin.get(f"{SELECTIONS}/{made}").json()["query"] == {"kind": ["manga"]}


def test_a_stored_query_that_is_not_the_grammar_selects_nothing(world: World) -> None:
    """A row written under the application's feet (a restore, a hand edit): never evaluated."""
    admin = world.env.client
    made = _make(world, admin, "tampered", {"kind": ["manga"]})
    for text in ('{"kind":["manga"],"sql":"1=1"}', '{"kind":["x\' OR 1=1 --"]}', '{"shelf":["a"]}'):
        with world.seed.engine.begin() as c:
            c.execute(sa.text("UPDATE saved_selection SET query_json = :q"), {"q": text})
        answer = admin.get(f"{SELECTIONS}/{made}")
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert (body["items"], body["total"], body["valid"]) == ([], 0, False), text
        assert admin.get(SELECTIONS).status_code == 200


def test_the_number_of_selections_of_a_profile_is_bounded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.catalog.application import selections

    monkeypatch.setattr(selections, "MAX_SELECTIONS", 3)
    admin = world.env.client
    for index in range(3):
        _make(world, admin, f"s{index}", {"kind": ["manga"]})
    full = _post(world, admin, SELECTIONS, {"name": "one more", "query": {"kind": ["manga"]}})
    assert full.status_code == 409 and full.json()["code"] == "selection.full"
    other = _member(world)
    assert (
        _post(world, other, SELECTIONS, {"name": "mine", "query": {"kind": ["a"]}}).status_code
        == 201
    )


def test_the_evaluation_is_bounded_in_results_and_in_time(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.catalog.application import selections

    admin = world.env.client
    made = _make(world, admin, "all", {"kind": [_kind(world)]})
    assert admin.get(f"{SELECTIONS}/{made}", params={"limit": 100_000}).status_code == 422
    assert admin.get(f"{SELECTIONS}/{made}", params={"limit": 0}).status_code == 422
    assert admin.get(f"{SELECTIONS}/{made}", params={"offset": -1}).status_code == 422
    assert len(admin.get(f"{SELECTIONS}/{made}", params={"limit": 200}).json()["items"]) <= 200
    # the count stops where it is told to, and says so
    monkeypatch.setattr(selections, "COUNT_CAP", 4)
    capped = admin.get(f"{SELECTIONS}/{made}").json()
    assert (capped["total"], capped["capped"]) == (4, True)
    monkeypatch.undo()
    # a query that runs out of time is a clean refusal, never a hang or a 500
    monkeypatch.setattr(selections, "EVAL_BUDGET_SECONDS", -1.0)
    monkeypatch.setattr(selections, "PROGRESS_STEP", 1)
    slow = admin.get(f"{SELECTIONS}/{made}")
    assert slow.status_code == 503 and slow.json()["code"] == "selection.too_slow"
    monkeypatch.undo()
    assert admin.get(f"{SELECTIONS}/{made}").status_code == 200  # the connection is clean again


# -- the selection of a profile is hers --------------------------------------------------------


def test_member_cannot_read_another_profile_selection(world: World) -> None:
    kid, admin = world.kid, world.env.client
    mum = _member(world)
    theirs = _make(world, kid, "Kid's own", {"kind": [_kind(world)]})
    absent = mum.get(f"{SELECTIONS}/{ABSENT}")
    assert absent.status_code == 404
    # a member, and the admin too: another profile's selection is one that does not exist
    for client in (mum, admin):
        got = client.get(f"{SELECTIONS}/{theirs}")
        assert _shape(got) == _shape(absent)
        assert theirs not in client.get(SELECTIONS).text
        assert _shape(
            _put(world, client, f"{SELECTIONS}/{theirs}", {"name": "mine now"})
        ) == _shape(absent)
        assert _shape(_delete(world, client, f"{SELECTIONS}/{theirs}")) == _shape(absent)
    assert [r.name for r in _rows(world)] == ["Kid's own"]  # nothing changed, nothing removed
    # and the other way round: the child reads nothing of a member's
    hers = _make(world, mum, "Hers", {"kind": [_kind(world)]})
    assert _shape(kid.get(f"{SELECTIONS}/{hers}")) == _shape(absent)
    assert hers not in kid.get(SELECTIONS).text
    # no sharing: a selection is made for the profile that makes it, whatever the body says
    for extra in ({"owner_user_id": _user_id(world, "mum")}, {"shared": True}, {"profile": "mum"}):
        made = _post(world, kid, SELECTIONS, {"name": "x", "query": {"kind": ["a"]}, **extra})
        assert made.status_code == 422
    assert {r.owner_user_id for r in _rows(world)} == {
        _user_id(world, "kid"),
        _user_id(world, "mum"),
    }


def test_a_key_reads_as_its_profile_and_writes_nothing(world: World) -> None:
    kid, auth = world.kid, local_auth(world.seed.engine, world.env.settings)
    mine = _make(world, kid, "Mine", {"kind": [_kind(world)]})
    mum = _member(world)
    hers = _make(world, mum, "Hers", {"kind": [_kind(world)]})

    def key(username: str, ip: str, **kwargs: Any) -> tuple[Any, dict[str, str]]:
        made = auth.create_api_key(name=f"{username}-key", username=username, **kwargs)
        return world.env.other_client(ip), {"Authorization": f"Bearer {made.value}"}

    kid_client, kid_auth = key("kid", "10.0.0.71", read_only=True)
    read = kid_client.get(f"{SELECTIONS}/{mine}", headers=kid_auth)
    assert read.status_code == 200
    world.clean(read.text)  # her key reads as her: the scope is a child's
    assert [i["id"] for i in kid_client.get(SELECTIONS, headers=kid_auth).json()["items"]] == [mine]
    assert _shape(kid_client.get(f"{SELECTIONS}/{hers}", headers=kid_auth)) == _shape(
        kid_client.get(f"{SELECTIONS}/{ABSENT}", headers=kid_auth)
    )
    # a key writes nothing here, read-only or not; and a key of the admin reads no one else's
    writer_client, writer_auth = key("kid", "10.0.0.72")
    for client, headers in ((kid_client, kid_auth), (writer_client, writer_auth)):
        attempts = (
            client.post(SELECTIONS, json={"name": "k", "query": {"kind": ["a"]}}, headers=headers),
            client.put(f"{SELECTIONS}/{mine}", json={"name": "k"}, headers=headers),
            client.delete(f"{SELECTIONS}/{mine}", headers=headers),
        )
        assert all(a.status_code in (401, 403) for a in attempts), [a.status_code for a in attempts]
    admin_client, admin_auth = key("admin", "10.0.0.73")
    assert _shape(admin_client.get(f"{SELECTIONS}/{mine}", headers=admin_auth)) == _shape(
        admin_client.get(f"{SELECTIONS}/{ABSENT}", headers=admin_auth)
    )
    assert sorted(r.name for r in _rows(world)) == ["Hers", "Mine"]  # nothing written by a key


def test_no_session_no_selection(world: World) -> None:
    anonymous = world.env.other_client("10.0.0.99")
    for response in (anonymous.get(SELECTIONS), anonymous.get(f"{SELECTIONS}/{ABSENT}")):
        assert response.status_code == 401


# -- the selection goes with its profile, is exported with her, and does not hold a Work ----------


def test_a_selection_is_exported_with_its_profile_and_goes_with_her(world: World) -> None:
    admin = world.env.client
    mum, kid = _member(world), world.kid
    _make(world, mum, "Hers", {"reading_state": ["unread"], "lang": ["fr"]})
    _make(world, kid, "Not hers", {"kind": ["manga"]})
    mum_id = _user_id(world, "mum")
    exported = admin.get(f"/api/v1/profiles/{mum_id}/export")
    assert exported.status_code == 200, exported.text
    section = exported.json()["saved_selections"]
    assert [(s["name"], s["query"]) for s in section] == [
        ("Hers", {"lang": ["fr"], "reading_state": ["unread"]})
    ]
    assert "Not hers" not in exported.text
    gone = _delete(world, admin, f"/api/v1/profiles/{mum_id}")
    assert gone.status_code == 204
    assert [r.name for r in _rows(world)] == ["Not hers"]  # hers went with her, nobody else's


def test_the_purge_of_a_work_leaves_the_selection_as_it_is(world: World) -> None:
    kid, works = world.kid, world.seed.works
    made = _make(
        world, kid, "Orchard", {"kind": [_kind(world)], "shelf": [world.seed.shelves["adult"]]}
    )
    before = [tuple(r) for r in _rows(world)]
    assert world.seed.title_of("ok") in _titles(kid.get(f"{SELECTIONS}/{made}"))
    with world.seed.engine.begin() as c:  # the Work leaves the catalogue (a purge)
        c.execute(sa.delete(catalog.work).where(catalog.work.c.id == works["ok"]))
    assert [tuple(r) for r in _rows(world)] == before  # it was only ever a query
    after = kid.get(f"{SELECTIONS}/{made}")
    assert after.status_code == 200 and world.seed.title_of("ok") not in _titles(after)
    assert after.json()["valid"] is True
    # and a shelf that goes does not break it either
    with world.seed.engine.begin() as c:
        c.execute(sa.text("UPDATE edition SET shelf_id = NULL"))
        c.execute(sa.text("DELETE FROM shelf WHERE id = :s"), {"s": world.seed.shelves["adult"]})
    assert kid.get(f"{SELECTIONS}/{made}").status_code == 200


def child_case(world: World) -> None:
    """The surface `selections` of the child-surface suite (`test_child_surfaces`): a selection she
    makes is evaluated in her scope, in its list, its count and its pages; what she cannot see is
    an absent one, and another profile's selection too."""
    kid, kind = world.kid, _kind(world)
    for query in (
        {"kind": [kind]},
        {"shelf": [world.seed.shelves["family"], world.seed.shelves["adult"]]},
        {"lang": ["fr", "en"], "reading_state": ["unread"]},
        {"added_within_days": 3650},
    ):
        made = _make(world, kid, "q", query)
        answer = kid.get(f"{SELECTIONS}/{made}")
        assert answer.status_code == 200, answer.text
        world.clean(answer.text)
        assert answer.json()["total"] == len(answer.json()["items"])
    world.clean(kid.get(SELECTIONS).text)
    theirs = _make(world, world.env.client, "Admin's", {"kind": [kind]})
    assert kid.get(f"{SELECTIONS}/{theirs}").status_code == 404
    assert theirs not in kid.get(SELECTIONS).text
