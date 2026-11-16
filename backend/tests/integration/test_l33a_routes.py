"""M4 L33a (EF-EXP-002, EF-USR-012, EF-USR-024): the two reads the screens need.

The shelves screen picks a server for a new binding: `GET /api/v1/targets` names the servers and
what each one keeps apart (admin session only). The Kavita account screen of a member lists the
Kavita bindings the profile may link a key to: `GET /api/v1/profiles/{id}/kavita-destinations`.
It answers like the routes of the keys do (an adult for themselves, the admin for a child, the
profile of another as 404, a child for themselves 403, an API key 403) and never carries a key,
a fingerprint or an account name: it says what can be linked, not what is.
"""

import json
from collections.abc import Iterator
from contextlib import closing

import pytest

from shelfhold.config import Settings
from tests.auth_env import run_cli
from tests.destination_account_env import (
    ULID_UNKNOWN,
    Fixture,
    accounts_env,
)
from tests.integration.test_destination_accounts import KEY, _member

TARGETS = "/api/v1/targets"


def choices_path(user_id: str) -> str:
    return f"/api/v1/profiles/{user_id}/kavita-destinations"


@pytest.fixture
def fx(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Fixture]:
    with accounts_env(monkeypatch, settings) as env:
        yield env


def _close_a_shelf_to(fx: Fixture, user_id: str) -> None:
    """The shelf of the first binding is opened to one profile only."""
    import sqlite3

    with closing(sqlite3.connect(fx.settings.database_path, isolation_level=None)) as db:
        shelf = db.execute(
            "SELECT shelf_id FROM shelf_destination WHERE id = ?", (fx.destination_id,)
        ).fetchone()[0]
        db.execute("UPDATE shelf SET allowed_profiles = ? WHERE id = ?", (json.dumps([user_id]), shelf))


# --- the servers a binding can name ------------------------------


@pytest.mark.req("EF-EXP-002")
def test_targets_list_names_each_server_and_what_it_keeps_apart(fx: Fixture) -> None:
    done = fx.api.client.get(TARGETS)
    assert done.status_code == 200, done.text
    items = {i["name"]: i for i in done.json()["items"]}
    assert set(items) == {"kavita-main", "plain-folder"}
    assert items["kavita-main"]["kind"] == "kavita"
    assert items["kavita-main"]["partitioning"] == "none"
    assert set(items["kavita-main"]) == {"id", "name", "kind", "partitioning"}  # no config, no key


@pytest.mark.req("EF-EXP-002", "EF-USR-024")
def test_targets_list_is_for_an_admin_session_only(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _member(fx, "mum")
    _member(fx, "kid", "child", max_age_rating=10)
    for username in ("mum", "kid"):
        fx.as_user(username)
        assert fx.api.client.get(TARGETS).status_code == 403, username
    for scope in ("read", "admin"):
        assert run_cli(monkeypatch, fx.settings, "admin", "keys", "create", "--name", scope,
                       "--scope", scope) == 0  # fmt: skip
        found = KEY.search(capsys.readouterr().out)
        assert found
        fx.api.client.cookies.clear()
        refused = fx.api.client.get(TARGETS, headers={"Authorization": f"Bearer {found.group(0)}"})
        assert refused.status_code == 403, scope
    fx.api.client.cookies.clear()
    assert fx.api.client.get(TARGETS).status_code == 401


# --- the bindings a profile may link a key to ------------------------------


@pytest.mark.req("EF-USR-012")
def test_a_member_lists_the_kavita_bindings_they_may_link(fx: Fixture) -> None:
    mum, key = _member(fx, "mum")
    fx.as_user("mum")
    done = fx.api.client.get(choices_path(mum))
    assert done.status_code == 200, done.text
    ids = [i["id"] for i in done.json()["items"]]
    # the two bindings to the Kavita; the one to a folder is not one a key opens
    assert sorted(ids) == sorted([fx.destination_id, fx.other_destination_id])
    first = next(i for i in done.json()["items"] if i["id"] == fx.destination_id)
    assert set(first) == {"id", "shelf_name", "target_name", "linked"}
    assert first["target_name"] == "kavita-main"
    assert first["linked"] is False
    assert fx.put(mum, fx.destination_id, key, "mum.kv").status_code == 200
    again = fx.api.client.get(choices_path(mum)).json()["items"]
    assert {i["id"]: i["linked"] for i in again}[fx.destination_id] is True
    assert key not in fx.api.client.get(choices_path(mum)).text


@pytest.mark.req("EF-USR-012", "EF-USR-024")
def test_a_shelf_closed_to_a_member_is_not_listed_to_them(fx: Fixture) -> None:
    mum, _ = _member(fx, "mum")
    dad, _ = _member(fx, "dad")
    _close_a_shelf_to(fx, dad)
    fx.as_user("mum")
    ids = [i["id"] for i in fx.api.client.get(choices_path(mum)).json()["items"]]
    assert fx.destination_id not in ids and fx.other_destination_id in ids
    fx.as_user("dad")
    ids = [i["id"] for i in fx.api.client.get(choices_path(dad)).json()["items"]]
    assert fx.destination_id in ids


@pytest.mark.req("EF-USR-012", "EF-USR-024")
def test_choices_idor_child_and_key(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mum, _ = _member(fx, "mum")
    dad, _ = _member(fx, "dad")
    kid, _ = _member(fx, "kid", "child", max_age_rating=10)
    fx.as_user("mum")
    for target in (dad, kid, ULID_UNKNOWN):  # another's profile and an invented one: one answer
        refused = fx.api.client.get(choices_path(target))
        assert refused.status_code == 404, target
    fx.as_user("kid")
    assert fx.api.client.get(choices_path(kid)).status_code == 403  # a child links nothing
    # the parent (an admin) reads the choices of a child, for the key they will enter
    fx.api.client.cookies.clear()
    assert fx.api.login().status_code == 200
    assert fx.api.client.get(choices_path(kid)).status_code == 200
    assert fx.api.client.get(choices_path(ULID_UNKNOWN)).status_code == 404
    for scope in ("read", "request", "admin"):
        argv = ["admin", "keys", "create", "--name", f"k-{scope}", "--scope", scope]
        if scope == "request":
            argv += ["--subject", "mum"]
        assert run_cli(monkeypatch, fx.settings, *argv) == 0
        found = KEY.search(capsys.readouterr().out)
        assert found
        fx.api.client.cookies.clear()
        header = {"Authorization": f"Bearer {found.group(0)}"}
        for target in (mum, ULID_UNKNOWN):
            assert fx.api.client.get(choices_path(target), headers=header).status_code == 403, scope
