"""Adversarial review of #208 (M3 L11, `?since=`): proofs of concept, red on the head of the PR.

Each test is one objection of `docs/decisions/M3.md` (« L11 : contradicteur de la #208 »)
and turns green when the objection is lifted. The production code is not touched here.
"""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.catalog.application import changes
from shelfhold.config import Settings
from shelfhold.events.since_key import KEY_FILE
from shelfhold.ops.backup.archive import FILES
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env
from tests.integration.test_since_tokens import SHELVES, World
from tests.visibility_env import add_profile, seed_catalogue


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


@pytest.fixture
def world(api: AuthEnv) -> Iterator[World]:
    """The world of `test_since_tokens`: a child (10), a child (16), a member and the admin."""
    seed = seed_catalogue(api)
    clients = {
        "kid": add_profile(api, "kid", "child", "10.0.0.2", max_age_rating=10),
        "kid16": add_profile(api, "kid16", "child", "10.0.0.3", max_age_rating=16),
        "member": add_profile(api, "member", "member", "10.0.0.4"),
    }
    assert api.login().status_code == 200
    yield World(api, seed, clients)
    seed.engine.dispose()


def _profile_id(world: World, username: str) -> str:
    answer = world.env.client.get("/api/v1/profiles")
    assert answer.status_code == 200, answer.text
    body: Any = answer.json()
    rows = body if isinstance(body, list) else next(v for v in body.values() if isinstance(v, list))
    return str(next(r["id"] for r in rows if r["username"] == username))


def _patch_profile(world: World, username: str, **body: object) -> None:
    client = world.env.client
    answer = client.patch(
        f"/api/v1/profiles/{_profile_id(world, username)}",
        json=body,
        headers={CSRF_HEADER: world.env.csrf(client)},
    )
    assert answer.status_code == 200, answer.text


def _position(world: World, token: str) -> int:
    """The position a token holds (opened with the key of the instance: a client cannot)."""
    from shelfhold.events.since_key import SinceKey
    from shelfhold.infrastructure.secrets import diagnostics
    from shelfhold.interfaces.http_api.lists import decode_since

    settings = world.env.settings
    key = SinceKey(lambda: diagnostics.boot_check(settings, create=False, announce=False)).value()
    return decode_since(token, key=key, kind="works").seq


def _head(world: World) -> int:
    with world.seed.engine.connect() as connection:
        return changes.window(connection).head


# -- B1: the token is not opaque ----------------------------------------------------------------


@pytest.mark.req("EF-INT-001", "EF-USR-006")
def test_review_poc_token_does_not_carry_the_sequence_in_clear(world: World) -> None:
    """`s2.<base64(decimal sequence)>.<mac>`: anyone reads the head of the household's event
    sequence (every event of every profile, plus one per Work created) out of a child's token."""
    token = world.token("kid")
    middle = token.split(".")[1]
    decoded = base64.urlsafe_b64decode(middle + "=" * (-len(middle) % 4))
    assert decoded != str(_head(world)).encode(), "the token spells the event sequence in clear"


@pytest.mark.req("EF-INT-001", "EF-USR-006")
def test_review_poc_token_does_not_reveal_activity_hidden_from_the_child(world: World) -> None:
    """Hidden activity (an unrated Work added, a 16+ Work raised to 18+) must not be told apart
    from no activity at all by comparing the child's tokens."""
    quiet_before = world.token("kid")
    quiet_after = world.token("kid")  # nothing happened in between
    world.new_work("Tale of the Parent's Shelf", age=None, rated=False)  # hidden from her
    assert world.rate("above", "18+").status_code == 200  # hidden before and after
    busy_after = world.token("kid")
    assert world.get("kid", since=quiet_after, limit=200).json()["removed"] == []
    assert (quiet_before == quiet_after) == (quiet_after == busy_after), (
        "the child's token moves on activity she cannot see: "
        f"{quiet_before!r} {quiet_after!r} {busy_after!r}"
    )


# -- B2: a loss of visibility that is not a change of the Work ---------------------------------


@pytest.mark.req("EF-INT-001", "EF-USR-006")
@pytest.mark.parametrize(
    ("username", "change", "lost"),
    [
        ("kid16", {"max_age_rating": 10}, "twelve"),  # the parent lowers her ceiling: 16 -> 10
        ("member", {"role": "child", "max_age_rating": 10}, "twelve"),  # an adult demoted
    ],
)
def test_review_poc_profile_change_reports_what_left_the_view(
    world: World, username: str, change: dict[str, object], lost: str
) -> None:
    first = world.get(username, limit=200).json()
    assert world.work(lost) in {i["work_id"] for i in first["items"]}  # she sees it at the token
    _patch_profile(world, username, **change)
    after = world.get(username, since=first["since"], limit=200)
    if after.status_code == 401:  # a role change ends her sessions: she logs in again, same client
        assert world.env.login(username, client=world.clients[username]).status_code == 200
        after = world.get(username, since=first["since"], limit=200)
    if after.status_code == 410:  # asking for a resync is an acceptable answer too
        return
    assert after.status_code == 200, after.text
    assert world.work(lost) not in {i["work_id"] for i in after.json()["items"]}
    assert world.work(lost) in after.json()["removed"], (
        "the Work left her view, the token does not say so: her offline client keeps it"
    )


# -- M1: a restored backup ----------------------------------------------------------------------


def _copy(source: str, target: str) -> None:
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


@pytest.mark.req("EF-INT-001", "EF-USR-006")
def test_review_poc_restore_in_place_never_replays_an_old_token(
    world: World, tmp_path: Any
) -> None:
    """The backup is restored, the key file stays (it lives in the data directory), the
    sequence starts again lower and passes the old token: the token is accepted and the
    removals written between the restored head and the token are skipped."""
    live = str(world.env.settings.database_path)
    saved = str(tmp_path / "backup.db")
    _copy(live, saved)  # the backup, taken at head H0
    for age in ("13+", "12+", "13+", "12+"):  # the household lives on: head H0 + n
        assert world.rate("twelve", age).status_code == 200
    token = world.token("kid")  # issued at H > H0; `ok` is hers
    assert world.work("ok") in {i["work_id"] for i in world.get("kid", limit=200).json()["items"]}
    _copy(saved, live)  # the restore: back to H0, same key
    assert _head(world) < _position(world, token)  # the restored head is behind the token
    assert world.rate("ok", "18+").status_code == 200  # H0 + 1 <= H: a loss for the child
    for age in ("13+", "12+", "13+", "12+", "13+", "12+"):  # the new history passes H
        assert world.rate("twelve", age).status_code == 200
    answer = world.get("kid", since=token, limit=200)
    if answer.status_code == 410:
        return
    assert world.work("ok") not in {i["work_id"] for i in answer.json()["items"]}
    assert world.work("ok") in answer.json()["removed"], "a token of another history was replayed"


@pytest.mark.req("EF-INT-001")
def test_review_poc_restored_backup_keeps_tokens_answerable_as_resync() -> None:
    """The doc promises `resync_required` (410) to the client of a restored instance. The key is
    not in the backup (`FILES`), nor derived from the keyring that is: a restore into a new
    data directory makes a new key, and every token answers `invalid_cursor` (400) instead."""
    assert KEY_FILE in FILES or "since" in " ".join(sorted(FILES)), (
        "the signing key of `since` tokens is not part of the backup"
    )


# -- m1: a production deletion of a shelf that writes no event ---------------------------------


@pytest.mark.req("EF-INT-001")
def test_review_poc_headless_rollback_of_a_shelf_is_a_removal(world: World) -> None:
    from shelfhold.headless import records
    from tests.visibility_env import _shelf

    with world.seed.engine.begin() as connection:
        made = _shelf(connection, "Made Then Rolled Back", None, [])
    first = world.env.client.get(SHELVES)
    assert made in {s["id"] for s in first.json()["items"]}
    with world.seed.engine.begin() as connection:  # `HeadlessAdmin._unbind(shelf_was_new=True)`
        records.unbind(connection, made, "no-destination", drop_shelf=True)
    after = world.env.client.get(SHELVES, params={"since": first.json()["since"]})
    assert after.status_code == 200
    assert made not in {s["id"] for s in after.json()["items"]}
    assert after.json()["removed"] == [made], "the admin saw the shelf, its removal is not told"
