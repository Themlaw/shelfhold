"""M4 L33b (EF-EXP-026, EF-USR-012, EF-USR-024): the state of the progress sync, member by member.

`GET /api/v1/sync-status` says, for each member and each Kavita binding open to them, whether a key
is linked, when the last pull and the last push were. A member reads their own rows and nobody
else's (an admin reads every member's), whatever they put in the query; a child and an API key read
nothing. The answer carries no key, no fingerprint and no Kavita account name.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

import pytest

from shelfhold.config import Settings
from tests.auth_env import run_cli
from tests.destination_account_env import Fixture, accounts_env
from tests.integration.test_destination_accounts import KEY, _link, _member

PATH = "/api/v1/sync-status"
PULL = datetime(2026, 11, 15, 11, 0, tzinfo=UTC)
PUSH = datetime(2026, 11, 15, 11, 5, tzinfo=UTC)


@pytest.fixture
def fx(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Fixture]:
    with accounts_env(monkeypatch, settings) as env:
        yield env


def _cursor(fx: Fixture, user_id: str, destination_id: str, *, push: bool = True) -> None:
    with closing(sqlite3.connect(fx.settings.database_path, isolation_level=None)) as db:
        db.execute(
            "INSERT INTO progress_sync_cursor (shelf_destination_id, user_id, last_pull_server_ts,"
            " last_push_at) VALUES (?, ?, ?, ?)",
            (destination_id, user_id, PULL.replace(tzinfo=None).isoformat(sep=" "),
             PUSH.replace(tzinfo=None).isoformat(sep=" ") if push else None),
        )  # fmt: skip


def _two_members_linked(fx: Fixture) -> tuple[str, str]:
    mum, mum_key = _member(fx, "mum")
    dad, dad_key = _member(fx, "dad")
    _link(fx, "mum", mum_key)
    _link(fx, "dad", dad_key)
    _cursor(fx, mum, fx.destination_id)
    _cursor(fx, dad, fx.destination_id, push=False)
    return mum, dad


def _rows(done: Any) -> list[dict[str, Any]]:
    assert done.status_code == 200, done.text
    return list(done.json()["items"])  # type: ignore[no-any-return]


@pytest.mark.req("EF-EXP-026", "EF-USR-012")
def test_sync_status_shows_only_own_member_to_non_admin(fx: Fixture) -> None:
    mum, dad = _two_members_linked(fx)
    fx.as_user("mum")
    rows = _rows(fx.api.client.get(PATH))
    assert {r["user_id"] for r in rows} == {mum}
    mine = next(r for r in rows if r["shelf_destination_id"] == fx.destination_id)
    assert mine["state"] == "linked"
    assert mine["last_pull_at"].startswith("2026-11-15T11:00")
    assert mine["last_push_at"].startswith("2026-11-15T11:05")
    # a parameter does not widen it: another's id is not a door
    for query in (f"?user_id={dad}", f"?member={dad}", f"?user={dad}&all=true"):
        assert {r["user_id"] for r in _rows(fx.api.client.get(PATH + query))} == {mum}, query
    fx.as_user("dad")
    other = _rows(fx.api.client.get(PATH))
    assert {r["user_id"] for r in other} == {dad}
    assert (
        next(r for r in other if r["shelf_destination_id"] == fx.destination_id)["last_push_at"]
        is None
    )  # never pushed


@pytest.mark.req("EF-EXP-026")
def test_admin_reads_every_member_and_names_the_ones_without_a_key(fx: Fixture) -> None:
    mum, dad = _two_members_linked(fx)
    nokey, _ = _member(fx, "nokey")
    fx.api.client.cookies.clear()
    assert fx.api.login().status_code == 200
    rows = _rows(fx.api.client.get(PATH))
    by_pair = {(r["user_id"], r["shelf_destination_id"]): r for r in rows}
    assert by_pair[(mum, fx.destination_id)]["state"] == "linked"
    assert by_pair[(dad, fx.destination_id)]["state"] == "linked"
    assert by_pair[(nokey, fx.destination_id)]["state"] == "no_token"
    assert by_pair[(nokey, fx.destination_id)]["last_pull_at"] is None
    # the binding to a folder is no Kavita: no row for it, for anyone
    assert fx.foreign_destination_id not in {r["shelf_destination_id"] for r in rows}
    # an account that was revoked is a member without a key again
    assert fx.send("DELETE", f"/api/v1/profiles/{mum}/destination-accounts/{fx.destination_id}"
                   ).status_code == 204  # fmt: skip
    again = {(r["user_id"], r["shelf_destination_id"]): r for r in _rows(fx.api.client.get(PATH))}
    assert again[(mum, fx.destination_id)]["state"] == "no_token"


@pytest.mark.req("EF-EXP-026", "EF-USR-024")
def test_sync_status_never_carries_a_key_a_fingerprint_or_an_account_name(fx: Fixture) -> None:
    mum, mum_key = _member(fx, "mum")
    fingerprint = _link(fx, "mum", mum_key)
    _cursor(fx, mum, fx.destination_id)
    for who in ("mum", None):
        if who:
            fx.as_user("mum")
        else:
            fx.api.client.cookies.clear()
            assert fx.api.login().status_code == 200
        text = fx.api.client.get(PATH).text
        assert mum_key not in text and fingerprint not in text and "mum.kv" not in text
        for row in json.loads(text)["items"]:
            assert set(row) == {
                "user_id", "username", "shelf_destination_id", "shelf_name", "target_name",
                "state", "last_pull_at", "last_push_at",
            }  # fmt: skip


@pytest.mark.req("EF-EXP-026", "EF-USR-024")
def test_a_shelf_closed_to_a_member_gives_no_row(fx: Fixture) -> None:
    _member(fx, "mum")
    dad, _ = _member(fx, "dad")
    with closing(sqlite3.connect(fx.settings.database_path, isolation_level=None)) as db:
        shelf = db.execute(
            "SELECT shelf_id FROM shelf_destination WHERE id = ?", (fx.destination_id,)
        ).fetchone()[0]
        db.execute("UPDATE shelf SET allowed_profiles = ? WHERE id = ?", (json.dumps([dad]), shelf))
    fx.as_user("mum")
    assert fx.destination_id not in {
        r["shelf_destination_id"] for r in _rows(fx.api.client.get(PATH))
    }
    fx.as_user("dad")
    assert fx.destination_id in {r["shelf_destination_id"] for r in _rows(fx.api.client.get(PATH))}


@pytest.mark.req("EF-EXP-026", "EF-USR-024")
def test_sync_status_child_key_and_anonymous_read_nothing(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_members_linked(fx)
    _member(fx, "kid", "child", max_age_rating=10)
    fx.as_user("kid")
    assert fx.api.client.get(PATH).status_code == 403
    for scope in ("read", "request", "admin"):
        argv = ["admin", "keys", "create", "--name", f"k-{scope}", "--scope", scope]
        if scope == "request":
            argv += ["--subject", "mum"]
        assert run_cli(monkeypatch, fx.settings, *argv) == 0
        found = KEY.search(capsys.readouterr().out)
        assert found
        fx.api.client.cookies.clear()
        refused = fx.api.client.get(PATH, headers={"Authorization": f"Bearer {found.group(0)}"})
        assert refused.status_code == 403, scope
    fx.api.client.cookies.clear()
    assert fx.api.client.get(PATH).status_code == 401


@pytest.mark.req("EF-EXP-026")
def test_a_member_without_a_key_reads_their_own_row_as_no_token(fx: Fixture) -> None:
    mum, _ = _member(fx, "mum")
    fx.as_user("mum")
    rows = _rows(fx.api.client.get(PATH))
    assert {r["state"] for r in rows} == {"no_token"}
    assert {r["shelf_destination_id"] for r in rows} == {fx.destination_id, fx.other_destination_id}
    assert {r["user_id"] for r in rows} == {mum}
