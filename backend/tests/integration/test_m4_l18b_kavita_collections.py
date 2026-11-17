"""M4 L18b (EF-EXP-025, ENF-SEC-010): collections and reading lists pushed to Kavita.

They are made under the service account of Shelfhold (the dedicated administrator key of the
target, never a member's key), then promoted. A child's Kavita account never sees a series it may
not: a collection goes only between libraries open to the same profiles, a profile's own list
carries her name and is promoted only when the admin said so and every series of it is in a library
open to her. What a person put there by hand is never withdrawn; what a shelf or a ceiling forbids
is withdrawn at the next pass, a pause notwithstanding; a process killed at any step leaves nothing
orphaned. The pipeline, the database and the registry are the production ones; Kavita is the
simulated server (no network).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.target_state import target_write_guard
from shelfhold.export.targets.kavita.collections import CollectionsSync
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.export_env import ExportEnv, Series, export_env
from tests.fakes.kavita.server import member
from tests.family_env import add_adult_shelf
from tests.integration.test_m4_l19_related_push import _library_of, indexed
from tests.kavita_env import run
from tests.kavita_export_env import Wired, wire

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
SERVICE = "admin"  # the account of the dedicated key in the simulated Kavita
CHILD_LIBRARY = 7  # the family shelf's library: the first of the simulated server
ADULT_LIBRARY = 8


class _Killed(BaseException):
    """The process dies (kill -9) at a step."""


class Scene:
    """A family shelf (a child's library) and an adult one, with a few indexed series."""

    def __init__(self, w: Wired) -> None:
        self.w = w
        self.env = w.env
        self.adult_shelf, self.adult_destination = add_adult_shelf(w.env)
        self.adult_library = _library_of(w.env, self.adult_destination)
        self.kid_one, self.kid_two = w.env.new_series("Kid one"), w.env.new_series("Kid two")
        self.adult_one = w.env.new_series("Grown one", shelf_id=self.adult_shelf)
        self.k_one = indexed(w, self.kid_one, "Kid one")
        self.k_two = indexed(w, self.kid_two, "Kid two")
        self.a_one = indexed(
            w, self.adult_one, "Grown one",
            destination_id=self.adult_destination, library_id=self.adult_library,
        )  # fmt: skip
        for series in w.fake.series:
            if series["id"] == self.a_one:
                series["libraryId"] = ADULT_LIBRARY
        w.fake.users.append(member("kid", libraries=(CHILD_LIBRARY,), age_rating=13))
        w.fake.users.append(member("grown", libraries=(CHILD_LIBRARY, ADULT_LIBRARY)))


def make(
    env: ExportEnv,
    name: str,
    works: list[Series],
    *,
    kind: str = "collection",
    owner: str | None = None,
    approved: bool = False,
) -> str:
    """A collection (or list) of Shelfhold, with its items in this order."""
    ident = new_ulid()
    with write_transaction(env.engine) as c:
        c.execute(
            sa.insert(catalog.collection).values(
                id=ident,
                kind=kind,
                name=name,
                owner_id=owner,
                public_approved_at=NOW if approved else None,
            )
        )
        for place, series in enumerate(works):
            c.execute(
                sa.insert(catalog.collection_item).values(
                    id=new_ulid(), collection_id=ident, position=place, work_id=series.work_id
                )
            )
    return ident


def add_user(env: ExportEnv, username: str, *, ceiling: int | None = None) -> str:
    ident = new_ulid()
    with write_transaction(env.engine) as c:
        c.execute(
            sa.text(
                'INSERT INTO "user" (id, username, role, max_age_rating) VALUES (:i, :u, :r, :m)'
            ),
            {"i": ident, "u": username, "r": "child" if ceiling else "member", "m": ceiling},
        )
    return ident


def sync(w: Wired) -> list[str]:
    done: list[str] = run(
        CollectionsSync(w.env.engine, w.env.clock, w.events).publish(w.client, w.env.target_id)
    )
    return done


def state(w: Wired) -> list[tuple[str, str, bool, list[int]]]:
    """What the service account holds in Kavita: (kind, title, promoted, series)."""
    shelves = w.fake.shelves
    held = [("collection", c["title"], c["promoted"], sorted(c["series"]))
            for c in shelves.collections.values() if c["owner"] == SERVICE]  # fmt: skip
    held += [
        ("list", x["title"], x["promoted"], sorted({i["seriesId"] for i in x["items"]}))
        for x in shelves.lists.values() if x["owner"] == SERVICE
    ]  # fmt: skip
    return sorted(held)


def seen(w: Wired, account: str) -> dict[str, dict[str, list[int]]]:
    return w.fake.shelves.seen_by(w.fake, account)


def pause(env: ExportEnv, w: Wired) -> None:
    with write_transaction(env.engine) as c:
        c.execute(sa.update(export.shelf).values(paused_reason="forbidden"))
    w.client.set_write_guard(target_write_guard(env.engine, env.target_id))


# -- the sync, under the service account, then promoted ----


@pytest.mark.req("EF-EXP-025")
def test_collections_reading_lists_sync() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        coll = make(env, "Cosy reads", [s.kid_one, s.kid_two])
        make(env, "Reading order", [s.kid_two, s.kid_one], kind="reading_list")
        assert sorted(sync(w)) != []
        assert state(w) == [
            ("collection", "Cosy reads", True, sorted([s.k_one, s.k_two])),
            ("list", "Reading order", True, sorted([s.k_one, s.k_two])),
        ]
        # the list is in the order of Shelfhold: the chapters of the second series first
        order = [i["seriesId"] for i in next(iter(w.fake.shelves.lists.values()))["items"]]
        assert list(dict.fromkeys(order)) == [s.k_two, s.k_one]
        assert w.fake.violations == []
        # a second pass sends nothing
        before = len(w.fake.requests)
        assert sync(w) == []
        assert len(w.fake.requests) == before
        # a series added, a series removed, a collection deleted: Kavita follows
        three = w.env.new_series("Kid three")
        k_three = indexed(w, three, "Kid three")
        with write_transaction(env.engine) as c:
            c.execute(
                sa.insert(catalog.collection_item).values(
                    id=new_ulid(), collection_id=coll, position=5, work_id=three.work_id
                )
            )
            c.execute(
                sa.delete(catalog.collection_item).where(
                    catalog.collection_item.c.collection_id == coll,
                    catalog.collection_item.c.work_id == s.kid_one.work_id,
                )
            )
        sync(w)
        assert ("collection", "Cosy reads", True, sorted([s.k_two, k_three])) in state(w)
        with write_transaction(env.engine) as c:
            c.execute(sa.delete(catalog.collection).where(catalog.collection.c.id == coll))
        sync(w)
        assert [t[1] for t in state(w)] == ["Reading order"]
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_kavita_collection_created_under_service_account_and_promoted() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        make(env, "Family picks", [s.kid_one, s.kid_two])
        sync(w)
        held = [c for c in w.fake.shelves.collections.values() if c["title"] == "Family picks"]
        assert len(held) == 1
        assert held[0]["owner"] == SERVICE and held[0]["promoted"] is True
        # every call was made by the account of the dedicated key: no member key anywhere
        assert {account for account, _ in w.fake.shelves.calls} == {SERVICE}
        # the restricted child account sees the collection and nothing but its own library's series
        assert seen(w, "kid")["collections"] == {"Family picks": sorted([s.k_one, s.k_two])}
        assert seen(w, "grown")["collections"] == {"Family picks": sorted([s.k_one, s.k_two])}
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_profile_list_not_promoted_if_a_series_is_closed_to_it() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        kid = add_user(env, "kid", ceiling=13)
        make(env, "Mine", [s.kid_one, s.adult_one], kind="reading_list", owner=kid, approved=True)
        sync(w)
        # it carries the profile's name, is the service account's, and stays out of sight
        assert [(t[0], t[1], t[2]) for t in state(w)] == [("list", "kid — Mine", False)]
        assert seen(w, "kid")["lists"] == {} and seen(w, "grown")["lists"] == {}
        # the series that was closed to her goes: the list is open to her, approved, and promoted
        with write_transaction(env.engine) as c:
            c.execute(
                sa.delete(catalog.collection_item).where(
                    catalog.collection_item.c.work_id == s.adult_one.work_id
                )
            )
        sync(w)
        assert [(t[1], t[2], t[3]) for t in state(w)] == [("kid — Mine", True, [s.k_one])]
        assert seen(w, "kid")["lists"] == {"kid — Mine": [s.k_one]}
        # a shelf narrowed to someone else closes the library to her: unpromoted at the next pass
        with write_transaction(env.engine) as c:
            c.execute(sa.update(export.shelf).values(allowed_profiles=[new_ulid()]))
        sync(w)
        assert [(t[1], t[2]) for t in state(w)] == [("kid — Mine", False)]
        assert seen(w, "kid")["lists"] == {}
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_a_private_list_is_not_even_pushed_without_the_admin() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        kid = add_user(env, "kid", ceiling=13)
        mine = make(env, "Mine", [s.kid_one], kind="reading_list", owner=kid)  # not approved
        sync(w)
        assert state(w) == [] and w.fake.shelves.lists == {}  # a private list stays in Shelfhold
        # the admin's word: pushed and promoted at the next pass
        with write_transaction(env.engine) as c:
            c.execute(
                sa.update(catalog.collection)
                .where(catalog.collection.c.id == mine)
                .values(public_approved_at=NOW)
            )
        sync(w)
        assert [(t[1], t[2]) for t in state(w)] == [("kid — Mine", True)]
        # taken back: out of Kavita altogether
        with write_transaction(env.engine) as c:
            c.execute(sa.update(catalog.collection).values(public_approved_at=None))
        sync(w)
        assert state(w) == []
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_what_a_restore_left_without_a_note_is_taken_out_of_sight_never_emptied() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        coll = make(env, "Cosy reads", [s.kid_one])
        sync(w)
        with write_transaction(env.engine) as c:  # the note is lost
            c.execute(sa.delete(catalog.collection_remote))
        with write_transaction(env.engine) as c:  # and the collection may no longer be promoted
            c.execute(
                sa.insert(catalog.collection_item).values(
                    id=new_ulid(), collection_id=coll, position=3, work_id=s.adult_one.work_id
                )
            )
        sync(w)
        assert state(w) == [
            ("collection", "Cosy reads", False, [s.k_one])
        ]  # not emptied, not taken
        assert seen(w, "kid")["collections"] == {}
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_an_object_that_holds_what_a_person_put_there_is_not_promoted() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        coll = make(env, "Cosy reads", [s.kid_one])
        sync(w)
        ours = next(c for c in w.fake.shelves.collections.values() if c["owner"] == SERVICE)
        assert ours["promoted"] is True
        ours["series"].append(s.a_one)  # a person adds an adult series by hand
        with write_transaction(env.engine) as c:  # the collection changes: the pass looks again
            c.execute(
                sa.insert(catalog.collection_item).values(
                    id=new_ulid(), collection_id=coll, position=4, work_id=s.kid_two.work_id
                )
            )
        sync(w)
        # the series stays (it is hers), but the audience rule has not seen it: out of sight
        assert ours["promoted"] is False and s.a_one in ours["series"]
        assert seen(w, "kid")["collections"] == {}
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_a_detachment_takes_back_what_was_pushed() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        make(env, "Cosy reads", [s.kid_one])
        make(env, "Order", [s.kid_one, s.kid_two], kind="reading_list")
        sync(w)
        hand = w.fake.shelves.by_hand_collection("grown", "By hand", [s.k_two])
        ours = next(c for c in w.fake.shelves.collections.values() if c["owner"] == SERVICE)
        ours["series"].append(s.k_two)  # a person put a series in one of them
        done = run(
            CollectionsSync(env.engine, env.clock, w.events).withdraw_all(w.client, env.target_id)
        )
        assert len(done) == 2
        assert [t[1:3] for t in state(w)] == [("Cosy reads", False)]  # kept for the person
        assert w.fake.shelves.collections[hand]["series"] == [s.k_two]
        assert seen(w, "kid")["lists"] == {} and seen(w, "kid")["collections"] == {}
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_a_collection_across_libraries_open_to_different_profiles_is_not_pushed() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        make(env, "Mixed", [s.kid_one, s.adult_one])
        make(env, "Grown only", [s.adult_one])
        assert sync(w) != []
        # the one that crosses is nowhere, not even cut down; the child's account is shown no name
        assert [t[1] for t in state(w)] == ["Grown only"]
        assert "Mixed" not in str(seen(w, "kid"))
        # the same shelves open to the same profiles: the crossing one goes
        with write_transaction(env.engine) as c:
            c.execute(sa.update(export.shelf).values(max_age_rating=None, allowed_profiles=[]))
        sync(w)
        assert sorted(t[1] for t in state(w)) == ["Grown only", "Mixed"]
        # and when they stop being open to the same profiles it is taken back whole
        with write_transaction(env.engine) as c:
            c.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == s.adult_shelf)
                .values(allowed_profiles=[new_ulid()])
            )
        sync(w)
        assert sorted(t[1] for t in state(w)) == ["Grown only"]
        assert w.fake.violations == []


# -- what a person did by hand is never withdrawn ----


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_what_a_person_put_in_kavita_by_hand_is_never_withdrawn() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        shelves = w.fake.shelves
        # a person's own collection, with the very title, under her account; one under the service
        mine = shelves.by_hand_collection("grown", "Cosy reads", [s.k_one])
        theirs = shelves.by_hand_list("grown", "Reading order", [s.k_two])
        coll = make(env, "Cosy reads", [s.kid_one, s.kid_two])
        make(env, "Reading order", [s.kid_one], kind="reading_list")
        sync(w)
        assert shelves.collections[mine]["series"] == [s.k_one]  # hers: not read, not touched
        assert [i["seriesId"] for i in shelves.lists[theirs]["items"]] == [s.k_two] * 2
        # a person adds a series to the collection of Shelfhold, and a chapter-set to its list
        ours = next(c for c in shelves.collections.values() if c["owner"] == SERVICE)
        a_hand = w.fake.add_series("By hand", "/manga/By hand", [])
        ours["series"].append(a_hand)
        the_list = next(x for x in shelves.lists.values() if x["owner"] == SERVICE)
        shelves.add_items(the_list["id"], a_hand)
        # Shelfhold drops its series, and then the whole collection
        with write_transaction(env.engine) as c:
            c.execute(
                sa.delete(catalog.collection_item).where(
                    catalog.collection_item.c.collection_id == coll
                )
            )
        sync(w)
        assert ours["series"] == [a_hand]
        with write_transaction(env.engine) as c:
            c.execute(sa.delete(catalog.collection))
        sync(w)
        # emptied of what was Shelfhold's, withdrawn from sight, but not deleted under her feet
        assert ours["id"] in shelves.collections and ours["promoted"] is False
        assert ours["series"] == [a_hand]
        assert {i["seriesId"] for i in the_list["items"]} == {a_hand}
        assert the_list["promoted"] is False
        assert shelves.collections[mine]["series"] == [s.k_one]
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-025")
def test_a_collection_of_the_same_title_under_the_service_account_is_not_taken_over() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        theirs = w.fake.shelves.by_hand_collection(SERVICE, "Cosy reads", [s.k_two])
        make(env, "Cosy reads", [s.kid_one])
        sync(w)
        # the title is taken by something Shelfhold did not make: left alone, nothing is pushed
        assert w.fake.shelves.collections[theirs]["series"] == [s.k_two]
        assert w.fake.shelves.collections[theirs]["promoted"] is False
        assert len(w.fake.shelves.collections) == 1
        assert w.fake.violations == []


# -- what a shelf or a ceiling forbids is withdrawn, a pause notwithstanding ----


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_what_became_forbidden_is_withdrawn_even_when_paused() -> None:
    with export_env(max_age_rating=None) as env:
        w = wire(env)
        s = Scene(w)
        with write_transaction(env.engine) as c:  # the two shelves open to the same profiles
            c.execute(sa.update(export.shelf).values(max_age_rating=None, allowed_profiles=[]))
        make(env, "Everything", [s.kid_one, s.adult_one])
        sync(w)
        assert [(t[1], t[2]) for t in state(w)] == [("Everything", True)]
        # the adult shelf is narrowed: the collection now crosses two audiences; paused as well
        with write_transaction(env.engine) as c:
            c.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == s.adult_shelf)
                .values(allowed_profiles=[new_ulid()])
            )
        pause(env, w)
        sync(w)
        assert seen(w, "kid")["collections"] == {}
        assert state(w) == []  # unpromoted, emptied and deleted: a withdrawal passes a pause
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-025")
def test_an_addition_waits_while_paused() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        make(env, "Cosy reads", [s.kid_one])
        pause(env, w)
        assert sync(w) == []
        assert w.fake.shelves.collections == {} and w.fake.shelves.lists == {}
        with write_transaction(env.engine) as c:
            c.execute(sa.update(export.shelf).values(paused_reason=None))
        sync(w)
        assert [t[1] for t in state(w)] == ["Cosy reads"]


@pytest.mark.req("EF-EXP-025")
def test_a_series_kavita_has_not_indexed_is_left_out_and_added_when_it_is() -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        unindexed = w.env.new_series("Not yet")
        make(env, "Cosy reads", [s.kid_one, unindexed])
        sync(w)
        assert state(w) == [("collection", "Cosy reads", True, [s.k_one])]
        later = indexed(w, unindexed, "Not yet")
        sync(w)
        assert state(w) == [("collection", "Cosy reads", True, sorted([s.k_one, later]))]
        # a collection with nothing of Kavita's in it is not created empty
        make(env, "Empty for now", [w.env.new_series("Never indexed")])
        sync(w)
        assert [t[1] for t in state(w)] == ["Cosy reads"]


# -- a kill -9 at any step leaves nothing orphaned ----


def _scenario(w: Wired) -> str:
    s = Scene(w)
    make(w.env, "Cosy reads", [s.kid_one, s.kid_two])
    make(w.env, "Reading order", [s.kid_two, s.kid_one], kind="reading_list")
    return "Cosy reads"


def _requests_of_a_clean_pass(withdraw: bool) -> tuple[int, int, list[Any]]:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        _scenario(w)
        sync(w)
        if withdraw:
            with write_transaction(env.engine) as c:
                c.execute(sa.delete(catalog.collection))
            start = len(w.fake.requests)
            sync(w)
            return len(w.fake.requests) - start, 0, state(w)
        return len(w.fake.requests), 0, state(w)


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
@pytest.mark.parametrize("withdraw", [False, True])
def test_kill_at_each_step_leaves_nothing_orphaned(withdraw: bool) -> None:
    total, _, clean = _requests_of_a_clean_pass(withdraw)
    assert total > 3
    for step in range(1, total + 1):  # the process dies before the `step`-th call of the pass
        with export_env(max_age_rating=13) as env:
            w = wire(env)
            _scenario(w)
            if withdraw:
                sync(w)
                with write_transaction(env.engine) as c:
                    c.execute(sa.delete(catalog.collection))
            start = len(w.fake.requests)

            def die(recorded: Any, step: int = step, start: int = start, w: Wired = w) -> None:
                if len(w.fake.requests) - start == step:
                    raise _Killed

            w.fake.hook = die
            with pytest.raises(_Killed):
                sync(w)
            w.fake.hook = None
            w.rebuild_client()
            sync(w)
            sync(w)
            assert state(w) == clean, f"killed before call {step}"
            # no two objects of one title, and nothing the database does not know about
            titles = [t[:2] for t in state(w)]
            assert len(titles) == len(set(titles))
            assert w.fake.violations == []


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_kill_between_kavitas_answer_and_the_registry_is_withdrawn_at_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        coll = make(env, "Cosy reads", [s.kid_one, s.kid_two])
        sync(w)
        # the collection shrinks; the process dies right after the series are removed
        with write_transaction(env.engine) as c:
            c.execute(
                sa.delete(catalog.collection_item).where(
                    catalog.collection_item.c.collection_id == coll,
                    catalog.collection_item.c.work_id == s.kid_two.work_id,
                )
            )

        real = CollectionsSync._record

        def killed_on(nth: int) -> Any:
            """The registry is written `nth` times, then the process dies: the call before it was
            Kavita's answer, the note after it never came."""
            calls: list[int] = []

            def record(self: Any, *args: Any, **kwargs: Any) -> None:
                calls.append(1)
                if len(calls) >= nth:
                    raise _Killed
                real(self, *args, **kwargs)

            return record

        with monkeypatch.context() as patch:
            patch.setattr(CollectionsSync, "_record", killed_on(1))
            with pytest.raises(_Killed):
                sync(w)
        sync(w)
        assert state(w) == [("collection", "Cosy reads", True, [s.k_one])]
        # the other way round: an addition cut short is noted too long, never too short
        with write_transaction(env.engine) as c:
            c.execute(
                sa.insert(catalog.collection_item).values(
                    id=new_ulid(), collection_id=coll, position=9, work_id=s.kid_two.work_id
                )
            )
        with monkeypatch.context() as patch:
            patch.setattr(CollectionsSync, "_record", killed_on(2))  # the note before, then death
            with pytest.raises(_Killed):
                sync(w)
        assert [t[3] for t in state(w)] == [sorted([s.k_one, s.k_two])]  # it did reach Kavita
        with write_transaction(env.engine) as c:
            c.execute(sa.delete(catalog.collection))
        sync(w)
        assert state(w) == []  # what was added before the registry was written is withdrawn too
        assert w.fake.violations == []
