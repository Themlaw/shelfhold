"""M3 L11 (EF-INT-001, volet M3, passes 12 et 13): `?since=` on the list of Works and of shelves.

The token is opaque, signed, backed by the sequence of the `event` table (never a time). What it
brings back is the removals: a Work that was deleted, a Work that left a reader's view (an admin
raised its age), limited to what that reader could see **at the time of the token**. Everything
goes through the real application: real sessions, CSRF, keys, and the real rating route.

The attacks come first: a child, a key, a member, a token carried to another reader, a token
carried to another list, a forged or truncated token, an approval without the right.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.catalog.application import changes
from shelfhold.catalog.public import visible_works_clause
from shelfhold.config import Settings
from shelfhold.identity.domain.visibility import scope_of
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.schema import catalog, platform
from shelfhold.interfaces.http_api.lists import decode_since, encode_since
from shelfhold.interfaces.http_api.problems import ProblemError
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env
from tests.visibility_env import (
    RATED,
    Seed,
    _work,
    add_profile,
    seed_catalogue,
)

WORKS = "/api/v1/works"
SHELVES = "/api/v1/shelves"
KEY = bytes(range(32))
CHANGE_TYPES = ("ops.work_visibility", "ops.work_removed")


class World:
    """The catalogue, a child (ceiling 10), another child (ceiling 16), a member and the admin."""

    def __init__(self, env: AuthEnv, seed: Seed, clients: dict[str, TestClient]) -> None:
        self.env, self.seed, self.clients = env, seed, clients

    def work(self, key: str) -> str:
        return self.seed.works[key]

    def get(self, who: str, path: str = WORKS, **params: Any) -> Any:
        client = self.env.client if who == "admin" else self.clients[who]
        return client.get(path, params={k: v for k, v in params.items() if v is not None})

    def token(self, who: str, path: str = WORKS) -> str:
        answer = self.get(who, path, limit=200)
        assert answer.status_code == 200, answer.text
        return str(answer.json()["since"])

    def removed(self, who: str, token: str) -> list[str]:
        answer = self.get(who, WORKS, since=token, limit=200)
        assert answer.status_code == 200, answer.text
        return list(answer.json()["removed"])

    def rate(self, key_or_id: str, age: str) -> Any:
        work_id = self.seed.works.get(key_or_id, key_or_id)
        return self.env.post(f"{WORKS}/{work_id}/rating", json={"age": age})

    def event_count(self) -> int:
        with self.seed.engine.connect() as connection:
            return int(
                connection.execute(
                    sa.select(sa.func.count()).select_from(platform.event)
                ).scalar_one()
            )

    def change_events(self) -> list[Any]:
        with self.seed.engine.connect() as connection:
            return list(
                connection.execute(
                    sa.select(platform.event)
                    .where(platform.event.c.type.in_(CHANGE_TYPES))
                    .order_by(platform.event.c.id)
                )
            )

    def new_work(self, title: str, *, age: int | None = 6, rated: bool = True) -> str:
        with self.seed.engine.begin() as connection:
            kind = str(
                connection.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one()
            )
            return _work(connection, kind, title, age=age, rated=rated)

    def clean(self, who: str, text: str, *, hidden: tuple[str, ...] = ()) -> None:
        ids = [self.seed.works[k] for k in hidden]
        titles = [self.seed.title_of(k) for k in hidden]
        leaked = [m for m in (*ids, *titles) if m in text]
        assert not leaked, f"{who} was shown {leaked}"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


@pytest.fixture
def world(api: AuthEnv) -> Iterator[World]:
    seed = seed_catalogue(api)
    clients = {
        "kid": add_profile(api, "kid", "child", "10.0.0.2", max_age_rating=10),
        "kid16": add_profile(api, "kid16", "child", "10.0.0.3", max_age_rating=16),
        "member": add_profile(api, "member", "member", "10.0.0.4"),
    }
    assert api.login().status_code == 200
    yield World(api, seed, clients)
    seed.engine.dispose()


def _key_for(world: World, username: str, *, read_only: bool = True) -> dict[str, str]:
    created = local_auth(world.seed.engine, world.env.settings).create_api_key(
        name=f"{username}-key", username=username, read_only=read_only
    )
    return {"Authorization": f"Bearer {created.value}"}


# -- the contract (the red tests of the card) ---------------------------------------------------


@pytest.mark.req("EF-INT-001")
def test_since_token_reports_removals_and_visibility_loss(world: World) -> None:
    gone = world.new_work("Tale of Vanished Pages")
    first = world.get("kid", limit=200).json()
    assert world.work("ok") in {i["work_id"] for i in first["items"]}
    assert first["removed"] == []  # no token given: nothing to report
    token = first["since"]
    adult_token = world.token("admin")

    assert world.rate("ok", "18+").status_code == 200  # visibility loss for the child
    with world.seed.engine.begin() as connection:  # a deletion
        changes.record_work_removed(connection, gone)
        connection.execute(sa.delete(catalog.work).where(catalog.work.c.id == gone))

    after = world.get("kid", since=token, limit=200).json()
    assert sorted(after["removed"]) == sorted([world.work("ok"), gone])
    assert world.work("ok") not in {i["work_id"] for i in after["items"]}
    assert after["since"] != token  # a fresh token, ready for the next call
    # an adult still sees the rated Work: only the deletion is a removal for her
    assert world.removed("admin", adult_token) == [gone]
    # the answer to the next call with the fresh token is empty
    assert world.removed("kid", after["since"]) == []


def _purge(world: World, *, days_ahead: int = 1000) -> None:
    """The real retention job, run far enough in the future to expire every event."""
    from datetime import UTC, datetime, timedelta

    from shelfhold.jobs.retention import RetentionPolicy, expire_events_and_jobs

    expire_events_and_jobs(
        world.seed.engine,
        now=datetime.now(UTC) + timedelta(days=days_ahead),
        policy=RetentionPolicy(),
    )


@pytest.mark.req("EF-INT-001")
def test_since_token_too_old_returns_resync_required(world: World) -> None:
    token = world.token("kid")
    assert world.rate("ok", "18+").status_code == 200
    _purge(world)  # the retention took the event that came after the token
    answer = world.get("kid", since=token)
    assert answer.status_code == 410
    assert answer.json()["type"] == "/api/v1/problems/resync-required"
    assert "removed" not in answer.text
    # a list without a token still works: this is how the client resynchronises
    fresh = world.get("kid", limit=200)
    assert fresh.status_code == 200
    assert world.removed("kid", fresh.json()["since"]) == []  # and its token is good


@pytest.mark.req("EF-INT-001")
def test_a_token_newer_than_what_the_purge_took_stays_good(world: World) -> None:
    assert world.rate("ok", "18+").status_code == 200
    _purge(world)  # everything up to here is gone
    token = world.token("kid")  # issued after the purge
    assert world.rate("twelve", "18+").status_code == 200
    assert world.rate("adult_shelf", "18+").status_code == 200
    assert world.removed("kid", token) == [world.work("adult_shelf")]
    # a purge that takes only events older than the token does not invalidate it
    with world.seed.engine.connect() as connection:
        assert changes.window(connection).floor < changes.window(connection).head


@pytest.mark.req("EF-INT-001")
def test_a_token_from_the_future_is_a_restore_and_asks_for_a_resync(world: World) -> None:
    from shelfhold.events.since_key import SinceKey

    key = SinceKey(world.env.settings.data_dir).value()
    ahead = encode_since(
        10**9, key=key, kind="works"
    )  # signed by this instance, but not issued yet
    answer = world.get("kid", since=ahead)
    assert answer.status_code == 410


@pytest.mark.req("EF-INT-001")
def test_since_removals_never_list_items_never_visible_to_viewer(world: World) -> None:
    token = world.token("kid")
    # hidden at the token, raised then lowered, lowered then deleted: never hers
    assert world.rate("unrated", "0+").status_code == 200  # becomes visible after the token
    assert world.rate("unrated", "18+").status_code == 200  # and leaves again
    assert world.rate("above", "18+").status_code == 200  # hidden before and after
    assert world.rate("x", "18+").status_code == 200
    hidden_gone = world.new_work("Tale of Hidden Ghost", age=None, rated=False)
    with world.seed.engine.begin() as connection:
        changes.record_work_removed(connection, hidden_gone)
        connection.execute(sa.delete(catalog.work).where(catalog.work.c.id == hidden_gone))
    shown_then_gone = world.new_work("Tale of Seen Then Gone")  # created after the token
    with world.seed.engine.begin() as connection:
        changes.record_work_removed(connection, shown_then_gone)
        connection.execute(sa.delete(catalog.work).where(catalog.work.c.id == shown_then_gone))

    answer = world.get("kid", since=token, limit=200)
    assert answer.status_code == 200
    assert answer.json()["removed"] == []  # nothing she saw at the token left
    world.clean(
        "kid", answer.text, hidden=("unrated", "above", "x", "flagged", "orphan", "misfiled")
    )
    assert hidden_gone not in answer.text and shown_then_gone not in answer.text


@pytest.mark.req("EF-INT-001")
def test_since_removals_follow_each_readers_own_ceiling(world: World) -> None:
    kid, kid16 = world.token("kid"), world.token("kid16")
    assert world.rate("twelve", "13+").status_code == 200  # hers (ceiling 16) both before and after
    assert world.rate("above", "18+").status_code == 200  # 16 -> 18+
    # ceiling 10 never saw `twelve` (12 > 10) nor `above`; ceiling 16 saw `above` and `twelve`
    assert world.removed("kid", kid) == []
    assert world.removed("kid16", kid16) == [world.work("above")]


@pytest.mark.req("EF-INT-001")
def test_since_token_tampered_rejected(world: World) -> None:
    token = world.token("kid")
    body = token.split(".")
    assert len(body) == 3 and body[0] == "s2"
    other = base64.urlsafe_b64encode(b"1").rstrip(b"=").decode()
    forged = [
        ".".join([body[0], body[1], body[2][:-1] + ("A" if body[2][-1] != "A" else "B")]),
        ".".join([body[0], other, body[2]]),  # another sequence, the same signature
        ".".join([body[0], body[1], ""]),
        ".".join(body[:2]),  # signature stripped
        "s1." + body[1],  # the unsigned M1a shape
        "",
        "s2.",
        "x" * 500,
        token.upper(),
        token + "=",
        "s2.é.é",
    ]
    for bad in forged:
        answer = world.get("kid", since=bad)
        assert answer.status_code == 400, bad
        assert answer.json()["type"] == "/api/v1/problems/invalid-cursor"
        assert "removed" not in answer.text


@pytest.mark.req("EF-INT-001")
def test_since_token_of_one_list_is_refused_on_the_other(world: World) -> None:
    shelf_token = world.token("admin", SHELVES)
    answer = world.env.client.get(WORKS, params={"since": shelf_token})
    assert answer.status_code == 400
    works_token = world.token("admin")
    assert world.env.client.get(SHELVES, params={"since": works_token}).status_code == 400


# -- who may ask --------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-001", "EF-USR-006")
def test_child_key_reads_removals_as_the_child_not_as_its_creator(world: World) -> None:
    key = _key_for(world, "kid")
    client = world.env.other_client("10.0.0.52")
    first = client.get(WORKS, params={"limit": 200}, headers=key)
    assert first.status_code == 200
    token = first.json()["since"]
    assert world.work("ok") in {i["work_id"] for i in first.json()["items"]}
    assert world.work("above") not in first.text
    assert world.rate("ok", "18+").status_code == 200
    assert world.rate("above", "18+").status_code == 200
    answer = client.get(WORKS, params={"since": token}, headers=key)
    assert answer.json()["removed"] == [world.work("ok")]
    world.clean("kid key", answer.text, hidden=("above", "x", "unrated"))


@pytest.mark.req("EF-INT-001", "EF-USR-006")
def test_token_carried_to_another_reader_is_filtered_for_that_reader(world: World) -> None:
    """IDOR: a token proves nothing about who holds it; the answer is the holder's own view."""
    admin_token = world.token("admin")
    assert world.rate("unrated", "0+").status_code == 200
    assert world.rate("unrated", "18+").status_code == 200
    assert world.rate("ok", "18+").status_code == 200
    assert world.rate("above", "18+").status_code == 200
    answer = world.get("kid", since=admin_token, limit=200)
    assert answer.status_code == 200
    assert answer.json()["removed"] == [world.work("ok")]  # not `unrated`, not `above`
    world.clean("kid", answer.text, hidden=("unrated", "above"))


@pytest.mark.req("EF-INT-001", "EF-USR-006")
def test_member_sees_every_removal_but_never_the_shelf_list(world: World) -> None:
    gone = world.new_work("Tale of Gone Quietly")
    token = world.token("member")
    assert world.rate("ok", "18+").status_code == 200
    with world.seed.engine.begin() as connection:
        changes.record_work_removed(connection, gone)
        connection.execute(sa.delete(catalog.work).where(catalog.work.c.id == gone))
    assert world.removed("member", token) == [gone]  # an adult sees the rated Work still
    assert world.get("member", SHELVES).status_code == 403


@pytest.mark.req("EF-INT-001", "EF-USR-006")
def test_shelf_list_is_closed_to_a_child_by_session_and_by_key(world: World) -> None:
    for who in ("kid", "kid16"):
        refused = world.get(who, SHELVES)
        assert refused.status_code == 403
        assert (
            world.get(who, SHELVES, since="s2.x.y").status_code == 403
        )  # refused before the token is read
    for username in ("kid", "member"):
        key = _key_for(world, username)
        assert world.env.other_client("10.0.0.53").get(SHELVES, headers=key).status_code == 403
    assert world.env.other_client("10.0.0.54").get(SHELVES).status_code == 401


@pytest.mark.req("EF-INT-001")
def test_works_list_needs_a_login(world: World) -> None:
    anonymous = world.env.other_client("10.0.0.55")
    assert anonymous.get(WORKS).status_code == 401
    assert anonymous.get(WORKS, params={"since": "s2.x.y"}).status_code == 401


@pytest.mark.req("EF-INT-001", "EF-BIB-036")
def test_rating_without_the_right_leaves_no_trace_in_the_sequence(world: World) -> None:
    """Approval without the right: a member, a key and a child cannot raise an age, and the
    attempt writes no event a token could later report."""
    before = world.event_count()
    work_id = world.work("ok")
    for who in ("member", "kid", "kid16"):
        client = world.clients[who]
        refused = client.post(
            f"{WORKS}/{work_id}/rating", json={"age": "18+"},
            headers={CSRF_HEADER: world.env.csrf(client)},
        )  # fmt: skip
        assert refused.status_code in (401, 403)
    for username, read_only in (("member", True), ("admin", False), ("admin", True)):
        key = _key_for(world, username, read_only=read_only)
        refused = world.env.other_client("10.0.0.56").post(
            f"{WORKS}/{work_id}/rating", json={"age": "18+"}, headers=key
        )
        assert refused.status_code in (401, 403)
    assert world.event_count() == before
    assert world.change_events() == []
    with world.seed.engine.connect() as connection:
        row = connection.execute(
            sa.select(catalog.work.c.age_rating).where(catalog.work.c.id == work_id)
        ).scalar_one()
    assert row == 6


# -- the writers ---------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-001")
def test_a_change_that_changes_nothing_writes_no_event(world: World) -> None:
    assert world.rate("twelve", "12+").status_code == 200  # the same age
    assert world.change_events() == []
    assert world.rate("ok", "18+").status_code == 200
    events = world.change_events()
    assert [e.type for e in events] == ["ops.work_visibility"]
    payload = events[0].payload
    assert payload["work_id"] == world.work("ok")
    assert payload["need_before"] == 6 and payload["need_after"] == 18
    assert set(payload) == {"work_id", "need_before", "need_after", "created_seq"}  # no title
    assert events[0].visibility == "ops"


@pytest.mark.req("EF-INT-001")
def test_the_age_of_a_new_edition_that_hides_a_work_is_a_visibility_loss(world: World) -> None:
    """A writer outside the rating route (a new Edition carrying an age above the ceiling)
    records its change too, through the same tracker."""
    token = world.token("kid")
    work_id = world.work("ok")
    from tests.visibility_env import _edition

    with world.seed.engine.begin() as connection, changes.track_work_changes(connection, [work_id]):
        _edition(connection, work_id, age=16, lang="de")
    assert world.removed("kid", token) == [work_id]


@pytest.mark.req("EF-INT-001")
def test_tracker_does_not_swallow_the_error_of_the_wrapped_write(world: World) -> None:
    work_id = world.work("ok")
    with (
        pytest.raises(RuntimeError, match="boom"),
        world.seed.engine.begin() as connection,
        changes.track_work_changes(connection, [work_id]),
    ):
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == work_id).values(age_rating=18)
        )
        raise RuntimeError("boom")
    assert world.change_events() == []  # the transaction rolled back, the event with it


@pytest.mark.req("EF-INT-001")
def test_removals_beyond_the_cap_ask_for_a_resync(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(changes, "MAX_REMOVED", 2)
    ids = [world.new_work(f"Tale of Crowd {n}") for n in range(3)]
    token = world.token("admin")
    with world.seed.engine.begin() as connection:
        for work_id in ids:
            changes.record_work_removed(connection, work_id)
            connection.execute(sa.delete(catalog.work).where(catalog.work.c.id == work_id))
    answer = world.get("admin", since=token)
    assert answer.status_code == 410  # more than a page of removals: reload the list


# -- one filter: the need of a Work is the filter ------------------------------------------------


@pytest.mark.req("EF-INT-001", "EF-USR-006")
def test_need_of_a_work_is_exactly_the_one_filter(world: World) -> None:
    """`work_need` answers "from which ceiling is this Work visible" by asking the filter of the
    catalogue itself, so the removals can never disagree with what a reader is shown."""
    with world.seed.engine.connect() as connection:
        for key, work_id in world.seed.works.items():
            need = changes.work_need(connection, work_id)
            for ceiling in range(0, 100):
                visible = work_id in {
                    r[0]
                    for r in connection.execute(
                        sa.select(catalog.work.c.id).where(
                            catalog.work.c.id == work_id,
                            visible_works_clause(scope_of("child", ceiling)),
                        )
                    )
                }
                assert visible == (ceiling >= need), (key, ceiling, need)
        assert changes.work_need(connection, "01HZZZZZZZZZZZZZZZZZZZZZZZ") == changes.NEED_NONE


# -- the token itself ---------------------------------------------------------------------------


@pytest.mark.req("EF-INT-001")
def test_since_token_round_trip_is_signed_and_scoped() -> None:
    token = encode_since(42, key=KEY, kind="works")
    assert token.startswith("s2.") and "42" not in token  # opaque, not a readable number
    assert decode_since(token, key=KEY, kind="works").seq == 42
    for key, kind in ((bytes(32), "works"), (KEY, "shelves")):
        with pytest.raises(ProblemError) as caught:
            decode_since(token, key=key, kind=kind)
        assert caught.value.spec.code == "invalid_cursor"
    assert re.fullmatch(r"s2\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token)
    assert encode_since(42, key=KEY, kind="works") == token  # deterministic
    assert encode_since(43, key=KEY, kind="works") != token


@pytest.mark.req("EF-INT-001")
def test_since_token_is_never_a_timestamp_and_bounds_hold() -> None:
    for bad in (-1, 2**63):
        with pytest.raises(ValueError):
            encode_since(bad, key=KEY, kind="works")
    assert decode_since(encode_since(0, key=KEY, kind="works"), key=KEY, kind="works").seq == 0
    assert (
        decode_since(encode_since(2**63 - 1, key=KEY, kind="works"), key=KEY, kind="works").seq
        == 2**63 - 1
    )


@pytest.mark.req("EF-INT-001")
def test_edition_merge_runs_in_the_tracker_and_records_only_a_real_change() -> None:
    """The survivor of a merge takes the strictest age. The merge is wrapped in the tracker: a
    merge that leaves the Work as visible as it was writes no event, and the Work stays a
    Work a younger reader sees for what it was."""
    from tests.family_env import NOW, family_env
    from tests.integration.test_l14_edition_merge import merge, pair

    with family_env(13) as env:
        p = pair(env)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.work)
                .where(catalog.work.c.id == p.series.work_id)
                .values(age_rating=0, age_x=False, rated_at=NOW, is_adult=False)
            )
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == p.donor)
                .values(age_rating=16, rated_at=NOW)
            )
            before = changes.work_need(connection, p.series.work_id)
        assert before == 16  # the donor's age already counts
        merge(p)
        with env.engine.connect() as connection:
            assert changes.work_need(connection, p.series.work_id) == before
            kinds = connection.execute(
                sa.select(platform.event.c.type).where(platform.event.c.type.in_(CHANGE_TYPES))
            ).all()
        assert kinds == []


# -- shelves, and the corners of the reader ---------------------------------------------------


@pytest.mark.req("EF-INT-001")
def test_admin_shelf_list_reports_removed_shelves_and_only_admins_read_it(world: World) -> None:
    from tests.visibility_env import _shelf

    with world.seed.engine.begin() as connection:
        doomed = _shelf(connection, "Doomed", None, [])
    first = world.env.client.get(SHELVES)
    assert first.status_code == 200
    shelves = {s["id"]: s for s in first.json()["items"]}
    assert shelves[world.seed.shelves["family"]]["max_age_rating"] == 13
    assert doomed in shelves and first.json()["removed"] == []
    assert "allowed_profiles" not in first.text  # the list says a shelf exists, not who may see it
    with world.seed.engine.begin() as connection:
        changes.record_removed(connection, "shelf", doomed)
        connection.execute(sa.text("DELETE FROM shelf WHERE id = :id"), {"id": doomed})
    after = world.env.client.get(SHELVES, params={"since": first.json()["since"]})
    assert after.status_code == 200
    assert after.json()["removed"] == [doomed]
    assert doomed not in {s["id"] for s in after.json()["items"]}
    assert (
        world.env.client.get(SHELVES, params={"since": after.json()["since"]}).json()["removed"]
        == []
    )
    # the member and the child never reach it, and a shelf event never shows in a list of Works
    assert world.get("member", SHELVES, since=first.json()["since"]).status_code == 403
    assert world.removed("admin", world.token("admin")) == []


@pytest.mark.req("EF-INT-001")
def test_a_scope_nobody_defined_is_told_nothing_and_foreign_events_are_ignored(
    world: World,
) -> None:
    from shelfhold.identity.domain.visibility import NOBODY

    with world.seed.engine.begin() as connection:
        head = changes.window(connection).head  # the seed exists at this token
        changes.record_work_removed(connection, world.work("ok"))
        for payload in ({}, {"work_id": 5, "need_before": 1}, {"work_id": "x", "need_before": "1"}):
            connection.execute(
                sa.insert(platform.event).values(
                    type="ops.work_removed", schema_version=1, payload=payload,
                    occurred_at=RATED, visibility="ops",
                )
            )  # fmt: skip
        assert changes.removed_ids(connection, NOBODY, head) == []
        assert changes.removed_ids(connection, scope_of("admin", None), head) == [world.work("ok")]
        with changes.track_work_changes(connection, []):
            pass  # nothing to track, nothing written
    assert [e.type for e in world.change_events()].count("ops.work_removed") == 4


@pytest.mark.req("EF-INT-001")
def test_the_event_schema_version_is_the_one_of_the_bus() -> None:
    from shelfhold.events.bus import SCHEMA_VERSION

    assert changes.EVENT_SCHEMA_VERSION == SCHEMA_VERSION


@pytest.mark.req("EF-INT-001", "EF-USR-016")
def test_a_child_never_reads_the_change_events_in_the_activity_feed(world: World) -> None:
    assert world.rate("ok", "18+").status_code == 200
    assert world.change_events()  # an event exists, for the admin's feed
    for who in ("kid", "kid16"):
        answer = world.get(who, "/api/v1/activity", limit=100)
        assert answer.status_code == 200
        assert "ops.work_visibility" not in answer.text and world.work("ok") not in answer.text
