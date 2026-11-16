"""M4 L19 (EF-BIB-039, ENF-SEC-010): related Works, the admin's API and what a profile is shown.

A relation links two Works that stay two: never a merge, never an Edition moved. The admin makes
and removes it (an admin session, or a key of scope `admin`, as `plans`); every profile reads the
relations of a Work she may see, and of those only the ones whose other end she may see too: not
the end, not the count, not the kind, not the title. A Work she may not see answers exactly like
a Work that does not exist. Real application, real sessions, real keys.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import apply_work_merge, plan_work_merge
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog
from tests.auth_env import ADMIN, CSRF_HEADER
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    HIDDEN_FROM_CEILING_10,
    MISSING,
    World,
    _shape,
    api,
    world,
)
from tests.integration.test_m4_l18a_collections import _key_client
from tests.visibility_env import _edition, _work, add_profile

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def _path(work: str) -> str:
    return f"/api/v1/works/{work}/relations"


def _relate(
    world: World, client: Any, work: str, other: str, kind: str = "sequel_of", **extra: Any
) -> Any:
    return client.post(
        _path(work),
        json={"to_work_id": other, "kind": kind},
        headers={CSRF_HEADER: world.env.csrf(client), **extra},
    )


def _unrelate(world: World, client: Any, work: str, kind: str, other: str, **extra: Any) -> Any:
    return client.delete(
        f"{_path(work)}/{kind}/{other}", headers={CSRF_HEADER: world.env.csrf(client), **extra}
    )


def _shown(answer: Any) -> list[tuple[str, str, bool]]:
    return [(i["work_id"], i["kind"], i["outgoing"]) for i in answer.json()["items"]]


def _rows(world: World) -> list[tuple[str, str, str]]:
    with world.seed.engine.connect() as c:
        return sorted(
            (r.from_work_id, r.to_work_id, r.kind)
            for r in c.execute(sa.select(catalog.work_relation))
        )


def _merge(world: World, keep: str, drop: str) -> None:
    with write_transaction(world.seed.engine) as c:
        plan = plan_work_merge(c, keep, drop)
        assert plan.mergeable, plan.blockers
        apply_work_merge(c, keep, drop, confirm=plan.fingerprint, now=NOW)


def _twin(world: World, title: str, lang: str) -> str:
    """A Work of its own, with one Edition on the open shelf, in a language no seeded Work uses
    (so that two of them can be merged)."""
    with write_transaction(world.seed.engine) as c:
        kind = str(c.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one())
        work = _work(c, kind, title, age=6)
        _edition(c, work, lang=lang, shelf_id=world.seed.shelves["adult"])
    return work


def _reserved_work(world: World, title: str) -> str:
    """A Work under her ceiling whose only Edition is on a shelf reserved to someone else."""
    with write_transaction(world.seed.engine) as c:
        kind = str(c.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one())
        work = _work(c, kind, title, age=6)
        _edition(c, work, shelf_id=world.seed.shelves["family"])  # allowed: not the kid
    return work


# -- the relation, on both sheets, and never a merge -------------------------------------------


@pytest.mark.req("EF-BIB-039")
def test_relation_visible_on_both_sheets(world: World) -> None:
    works, editions = world.seed.works, world.seed.editions
    before = (
        world.seed.engine.connect().execute(sa.text("SELECT count(*) FROM work")).scalar_one(),
        world.seed.engine.connect().execute(sa.text("SELECT count(*) FROM edition")).scalar_one(),
    )
    # the adaptation is the one that names the novel it comes from: from -> to
    made = _relate(world, world.env.client, works["adult_shelf"], works["ok"], "adaptation_of")
    assert made.status_code == 201, made.text
    assert made.json() == {"created": True}
    again = _relate(world, world.env.client, works["adult_shelf"], works["ok"], "adaptation_of")
    assert again.status_code == 200 and again.json() == {"created": False}
    # each end reads the relation, from its own side
    from_side = world.env.client.get(_path(works["adult_shelf"]))
    to_side = world.env.client.get(_path(works["ok"]))
    assert _shown(from_side) == [(works["ok"], "adaptation_of", True)]
    assert _shown(to_side) == [(works["adult_shelf"], "adaptation_of", False)]
    assert from_side.json()["count"] == to_side.json()["count"] == 1
    # the two sheets of the admin say it too
    for edition, other in (
        (editions["adult_shelf"], works["ok"]),
        (editions["ok"], works["adult_shelf"]),
    ):
        sheet = world.env.client.get(f"/api/v1/editions/{edition}/sheet")
        assert sheet.status_code == 200, sheet.text
        assert [r["work_id"] for r in sheet.json()["relations"]] == [other]
    # never a merge: two Works and their Editions, exactly as they were
    after = (
        world.seed.engine.connect().execute(sa.text("SELECT count(*) FROM work")).scalar_one(),
        world.seed.engine.connect().execute(sa.text("SELECT count(*) FROM edition")).scalar_one(),
    )
    assert after == before
    # the reader sees the same, as a profile that may see both
    assert _shown(world.kid.get(_path(works["ok"]))) == [
        (works["adult_shelf"], "adaptation_of", False)
    ]
    # and the relation goes with the removal, from either end of the call
    assert (
        _unrelate(
            world, world.env.client, works["adult_shelf"], "adaptation_of", works["ok"]
        ).status_code
        == 204
    )
    assert (
        _unrelate(
            world, world.env.client, works["adult_shelf"], "adaptation_of", works["ok"]
        ).status_code
        == 404
    )
    assert world.env.client.get(_path(works["ok"])).json()["items"] == []


# -- a child never sees a relation to a Work she does not see ----------------------------------


@pytest.mark.req("EF-BIB-039", "EF-USR-006", "ENF-SEC-010")
def test_child_never_sees_relation_to_hidden_work(world: World) -> None:
    works = world.seed.works
    reserved = _reserved_work(world, "Tale of Reserved Shelf")
    for key in sorted(HIDDEN_FROM_CEILING_10):  # to and from every hidden Work
        assert _relate(world, world.env.client, works["ok"], works[key]).status_code == 201
        assert (
            _relate(world, world.env.client, works[key], works["ok"], "spin_off_of").status_code
            == 201
        )
    assert _relate(world, world.env.client, works["ok"], reserved).status_code == 201
    assert _relate(world, world.env.client, works["ok"], works["adult_shelf"]).status_code == 201

    shown = world.kid.get(_path(works["ok"]))
    assert shown.status_code == 200
    assert _shown(shown) == [(works["adult_shelf"], "sequel_of", True)]
    assert shown.json()["count"] == 1  # the count is the count of what she is shown
    world.clean(shown.text)
    assert reserved not in shown.text and "Reserved Shelf" not in shown.text
    # the admin sees them all
    assert (
        world.env.client.get(_path(works["ok"])).json()["count"]
        == len(HIDDEN_FROM_CEILING_10) * 2 + 2
    )
    # the sheet of a hidden Work is an absent one, with the same body as an unknown identifier
    absent = world.kid.get(_path(MISSING))
    assert absent.status_code == 404
    for key in sorted(HIDDEN_FROM_CEILING_10):
        answer = world.kid.get(_path(works[key]))
        assert answer.status_code == 404 and _shape(answer) == _shape(absent), key
    assert world.kid.get(_path(reserved)).status_code == 404
    # a key reads as its profile, the child's included
    client, auth = _key_client(world, "kid", "10.0.0.81", read_only=True)
    keyed = client.get(_path(works["ok"]), headers=auth)
    assert keyed.status_code == 200 and keyed.json() == shown.json()
    # a read key of the admin reads as the admin's profile: the whole catalogue, no more
    admin_client, admin_auth = _key_client(world, ADMIN, "10.0.0.82", read_only=True)
    assert admin_client.get(_path(works["ok"]), headers=admin_auth).json()["count"] == shown.json()[
        "count"
    ] + (len(HIDDEN_FROM_CEILING_10) * 2 + 1)


@pytest.mark.req("EF-BIB-039", "EF-USR-006", "ENF-SEC-010")
def test_a_ceiling_lowered_or_a_work_rerated_takes_effect_on_the_next_read(world: World) -> None:
    works = world.seed.works
    assert _relate(world, world.env.client, works["ok"], works["adult_shelf"]).status_code == 201
    assert world.kid.get(_path(works["ok"])).json()["count"] == 1
    with write_transaction(world.seed.engine) as c:  # the admin re-rates it above her ceiling
        c.execute(
            sa.update(catalog.work)
            .where(catalog.work.c.id == works["adult_shelf"])
            .values(age_rating=16)
        )
    after = world.kid.get(_path(works["ok"]))
    assert after.status_code == 200 and after.json()["count"] == 0
    assert world.kid.get(_path(works["adult_shelf"])).status_code == 404


# -- the doors of the admin --------------------------------------------------------------------


@pytest.mark.req("EF-BIB-039", "ENF-SEC-010")
def test_relation_routes_refused_to_child_member_and_non_admin_key(world: World) -> None:
    works = world.seed.works
    member = add_profile(world.env, "mia", "member", "10.0.0.31")
    made = _relate(world, world.env.client, works["ok"], works["adult_shelf"])
    assert made.status_code == 201
    expected = _rows(world)
    absent_ids = (MISSING, works["above"], works["ok"])  # a refusal never tells which exist
    for who, client in (("child", world.kid), ("member", member)):
        for other in absent_ids:
            refused = _relate(world, client, works["ok"], other)
            assert refused.status_code == 403, (who, refused.text)
            assert _relate(world, client, MISSING, other).status_code == 403
            gone = _unrelate(world, client, works["ok"], "sequel_of", works["adult_shelf"])
            assert gone.status_code == 403, who
    # keys: read-only, request-scope, a member's key and a child's key, whatever the scope
    for name, ip, kwargs in (
        (ADMIN, "10.0.0.41", {"read_only": True}),
        (ADMIN, "10.0.0.42", {"scope": "request", "subject_username": "mia"}),
        ("mia", "10.0.0.43", {"read_only": True}),
        ("kid", "10.0.0.44", {"read_only": True}),
    ):
        client, auth = _key_client(world, name, ip, **kwargs)
        refused = client.post(
            _path(works["ok"]),
            json={"to_work_id": works["mixed"], "kind": "sequel_of"},
            headers=auth,
        )
        assert refused.status_code in (401, 403), (name, kwargs)
        deleted = client.delete(
            f"{_path(works['ok'])}/sequel_of/{works['adult_shelf']}", headers=auth
        )
        assert deleted.status_code in (401, 403), (name, kwargs)
    anonymous = world.env.other_client("10.0.0.45")
    assert (
        anonymous.post(
            _path(works["ok"]), json={"to_work_id": works["mixed"], "kind": "sequel_of"}
        ).status_code
        == 401
    )
    assert _rows(world) == expected  # nothing was written, nothing was removed
    # the admin's own key of scope `admin` is the one key that writes
    client, auth = _key_client(world, ADMIN, "10.0.0.46")
    ok = client.post(
        _path(works["ok"]), json={"to_work_id": works["mixed"], "kind": "spin_off_of"}, headers=auth
    )
    assert ok.status_code == 201, ok.text
    assert (
        client.delete(
            f"{_path(works['ok'])}/spin_off_of/{works['mixed']}", headers=auth
        ).status_code
        == 204
    )


@pytest.mark.req("EF-BIB-039", "ENF-SEC-010")
def test_a_relation_needs_two_known_distinct_works_and_a_known_kind(world: World) -> None:
    works = world.seed.works
    admin = world.env.client
    assert _relate(world, admin, works["ok"], works["ok"]).status_code == 422  # not with itself
    assert _relate(world, admin, works["ok"], MISSING).status_code == 404
    assert _relate(world, admin, MISSING, works["ok"]).status_code == 404
    assert _relate(world, admin, works["ok"], works["mixed"], "merged_into").status_code == 422
    assert _relate(world, admin, works["ok"], "not-an-id").status_code in (404, 422)
    assert _rows(world) == []


# -- a cycle is a contradiction, not a loop ----------------------------------------------------


@pytest.mark.req("EF-BIB-039")
def test_a_cyclic_relation_is_refused_and_never_loops(world: World) -> None:
    works = world.seed.works
    a, b, c = works["ok"], works["adult_shelf"], works["mixed"]
    admin = world.env.client
    assert _relate(world, admin, b, a, "sequel_of").status_code == 201  # b follows a
    refused = _relate(world, admin, a, b, "sequel_of")  # a follows b: it cannot
    assert refused.status_code == 409 and refused.json()["code"] == "relation.cycle"
    # "prequel_of" says the same thing the other way round: a is the prequel of b is the fact
    # already there, b is the prequel of a is the cycle
    assert _relate(world, admin, a, b, "prequel_of").status_code == 201
    assert _relate(world, admin, b, a, "prequel_of").status_code == 409
    assert _relate(world, admin, c, b, "sequel_of").status_code == 201  # c follows b
    assert _relate(world, admin, a, c, "sequel_of").status_code == 409  # a, b, c, a
    # other kinds are other graphs: an adaptation of its own sequel is odd, not a loop
    assert _relate(world, admin, a, b, "spin_off_of").status_code == 201
    assert _relate(world, admin, b, a, "spin_off_of").status_code == 409
    for work in (a, b, c):  # and every read still ends
        assert world.env.client.get(_path(work)).status_code == 200
    assert len(_rows(world)) == 4


# -- a Work merged or deleted -------------------------------------------------------------------


@pytest.mark.req("EF-BIB-039")
def test_a_relation_follows_a_merged_work_and_never_becomes_a_self_relation(world: World) -> None:
    works = world.seed.works
    keep, drop = _twin(world, "Tale of Survivor", "ja"), _twin(world, "Tale of Absorbed", "de")
    other, third = works["adult_shelf"], works["twelve"]
    admin = world.env.client
    assert _relate(world, admin, drop, other, "adaptation_of").status_code == 201
    assert _relate(world, admin, third, drop, "sequel_of").status_code == 201
    assert _relate(world, admin, drop, keep, "spin_off_of").status_code == 201  # keep <-> drop
    assert (
        _relate(world, admin, keep, other, "adaptation_of").status_code == 201
    )  # twin of the first
    _merge(world, keep, drop)
    rows = _rows(world)
    assert (keep, other, "adaptation_of") in rows
    assert (third, keep, "sequel_of") in rows  # moved to the survivor
    assert all(a != b for a, b, _k in rows)  # the relation of the two merged Works is gone
    assert not [r for r in rows if drop in r[:2]]
    assert len(rows) == 2  # the twin was not doubled
    # the old identifier leads to the survivor, for the read and for a new relation
    assert world.env.client.get(_path(drop)).json()["count"] == 2
    assert _shown(world.env.client.get(_path(keep))) == [
        (other, "adaptation_of", True),
        (third, "sequel_of", False),
    ]
    made = _relate(world, admin, works["above"], drop, "sequel_of")
    assert made.status_code == 201
    assert (works["above"], keep, "sequel_of") in _rows(world)
    # ... and it is not a relation of the Work with itself
    assert _relate(world, admin, keep, drop, "sequel_of").status_code == 422


@pytest.mark.req("EF-BIB-039", "ENF-SEC-010")
def test_a_deleted_work_is_an_absent_one_and_takes_its_relations_with_it(world: World) -> None:
    works = world.seed.works
    doomed, other = works["orphan"], works["ok"]
    assert _relate(world, world.env.client, other, doomed).status_code == 201
    with write_transaction(world.seed.engine) as c:
        c.execute(sa.delete(catalog.work).where(catalog.work.c.id == doomed))
    assert _rows(world) == []
    assert world.env.client.get(_path(doomed)).status_code == 404
    assert world.kid.get(_path(doomed)).status_code == 404
    assert world.env.client.get(_path(other)).json()["items"] == []
    assert _relate(world, world.env.client, other, doomed).status_code == 404


@pytest.mark.req("EF-BIB-039", "ENF-SEC-010")
def test_a_relation_to_a_work_merged_into_a_hidden_one_is_hidden_from_the_child(
    world: World,
) -> None:
    works = world.seed.works
    visible, hidden = works["ok"], works["above"]
    absorbed = _twin(world, "Tale of Absorbed Twin", "de")
    assert _relate(world, world.env.client, visible, absorbed).status_code == 201
    assert world.kid.get(_path(visible)).json()["count"] == 1
    _merge(world, hidden, absorbed)  # the twin joins a Work above her ceiling
    after = world.kid.get(_path(visible))
    assert after.status_code == 200 and after.json() == {
        "work_id": visible,
        "count": 0,
        "items": [],
    }
    assert world.kid.get(_path(absorbed)).status_code == 404  # the old id leads to a hidden Work
    world.clean(after.text)


def child_case(world: World) -> None:
    """The surface `relations` of the child-surface suite: what she is shown of a Work's relations
    is made of Works she may see, and what she cannot see is an absent one."""
    works = world.seed.works
    for key in sorted(HIDDEN_FROM_CEILING_10):
        assert _relate(world, world.env.client, works["ok"], works[key]).status_code == 201
    assert _relate(world, world.env.client, works["ok"], works["mixed"]).status_code == 201
    shown = world.kid.get(_path(works["ok"]))
    assert shown.status_code == 200 and shown.json()["count"] == 1
    world.clean(shown.text)
    absent = world.kid.get(_path(MISSING))
    for key in sorted(HIDDEN_FROM_CEILING_10):
        answer = world.kid.get(_path(works[key]))
        assert answer.status_code == 404 and _shape(answer) == _shape(absent), key
