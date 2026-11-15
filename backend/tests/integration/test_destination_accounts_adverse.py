"""M4 L12, relecture adverse de #281 : les trous trouvés, bouchés, et ce qui les garde fermés.

Chaque test décrit ce qui doit tenir. Aucune clé n'est littérale : le Kavita simulé les tire au
hasard.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from typing import Any

import httpx2
import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure import logging as shelfhold_logging
from tests.auth_env import ADMIN
from tests.destination_account_env import Fixture, accounts_env

PROFILES_PATH = "/api/v1/profiles"


@pytest.fixture
def fx(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Fixture]:
    registry = shelfhold_logging._known_values
    before = set(registry)
    try:
        with accounts_env(monkeypatch, settings) as env:
            yield env
    finally:  # what a test registered must not blank the lines of the next one
        registry.clear()
        registry.update(before)


def _member(fx: Fixture, username: str, role: str = "member", **kwargs: Any) -> tuple[str, str]:
    user_id = fx.add(username, role, **kwargs)
    return user_id, fx.fake.add_member_key(f"{username}.kv")


@pytest.mark.req("EF-USR-012")
@pytest.mark.parametrize("paste", ["event", "user_id", "url"])
def test_a_refused_paste_does_not_poison_the_known_secrets(fx: Fixture, paste: str) -> None:
    """Once, `MemberKey(token)` registered its value before any check: any string of 8 characters
    or more pasted by a member became a known secret for the life of the process, refused by the
    write guard (here the audit line of another member) and masked from every log."""
    _member(fx, "eve")
    _mum, mum_key = _member(fx, "mum")
    fx.as_user("eve")
    # not keys: an audit action name, an identifier, a URL; Kavita refuses them
    poison_value = {
        "event": "destination_account.linked",
        "user_id": fx.users["eve"],
        "url": "https://example.invalid/library",
    }[paste]
    poison = fx.put(fx.users["eve"], fx.destination_id, poison_value, "eve.kv")
    assert poison.status_code == 422
    fx.as_user("mum")
    done = fx.put(fx.users["mum"], fx.destination_id, mum_key, "mum.kv")
    assert done.status_code == 200, done.text
    assert poison_value not in shelfhold_logging.known_secret_values()


@pytest.mark.req("EF-USR-012", "EF-USR-017")
def test_a_changed_role_revokes_the_member_keys(fx: Fixture) -> None:
    """`set_role` révoque sessions et clés d'API (« a changed role starts again ») mais pas
    `destination_account` : un membre devenu enfant garde la clé d'un compte que le parent n'a
    jamais déclaré, et un enfant devenu adulte garde la clé saisie par le parent."""
    mum, mum_key = _member(fx, "mum")
    fx.as_user("mum")
    assert fx.put(mum, fx.destination_id, mum_key, "mum.kv").status_code == 200
    fx.as_user(ADMIN)
    changed = fx.send("PATCH", f"{PROFILES_PATH}/{mum}", {"role": "child", "max_age_rating": 10})
    assert changed.status_code == 200, changed.text
    assert fx.service.open_token(mum, fx.destination_id) is None


@pytest.mark.req("EF-USR-012", "EF-USR-017")
def test_a_child_takes_no_key_of_a_kavita_administrator(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le parent déclare pour l'enfant un compte Kavita qui est administrateur, puis colle sa clé :
    elle est scellée pour l'enfant, alors que `UserDto.roles` dit `Admin`."""
    kid, _ = _member(fx, "kid", "child", max_age_rating=10)
    admin_key = fx.fake.add_member_key("parent.kv")

    def admin_account(_recorded: Any) -> httpx2.Response:
        return httpx2.Response(200, json={"id": 1, "username": "parent.kv",
                                          "roles": ["Admin", "Login"]})  # fmt: skip

    monkeypatch.setattr(fx.fake, "_account", admin_account)
    fx.as_user(ADMIN)
    assert fx.send("PUT", f"{PROFILES_PATH}/{kid}/kavita-account",
                   {"kavita_username": "parent.kv"}).status_code == 200  # fmt: skip
    done = fx.put(kid, fx.destination_id, admin_key)
    assert done.status_code == 422, done.text
    assert fx.service.open_token(kid, fx.destination_id) is None


def _declare(fx: Fixture, kid: str, name: str) -> None:
    fx.as_user(ADMIN)
    done = fx.send("PUT", f"{PROFILES_PATH}/{kid}/kavita-account", {"kavita_username": name})
    assert done.status_code == 200, done.text


@pytest.mark.req("EF-USR-012", "EF-USR-017")
def test_a_changed_role_is_audited_and_frees_the_keys_of_a_child_turned_adult(fx: Fixture) -> None:
    kid, kid_key = _member(fx, "kid", "child", max_age_rating=10)
    _declare(fx, kid, "kid.kv")
    assert fx.put(kid, fx.destination_id, kid_key).status_code == 200
    changed = fx.send("PATCH", f"{PROFILES_PATH}/{kid}", {"role": "member"})
    assert changed.status_code == 200, changed.text
    assert fx.service.open_token(kid, fx.destination_id) is None
    assert ("destination_account.revoked", f"{kid}|{fx.destination_id}") in fx.audit()
    assert [row[4] for row in fx.accounts()] == [1]  # revoked, kept as a row, no secret left


@pytest.mark.req("EF-USR-012")
@pytest.mark.parametrize("short", ["a", "1234567"])
def test_a_key_shorter_than_eight_signs_is_refused(fx: Fixture, short: str) -> None:
    _member(fx, "mum")
    fx.as_user("mum")
    done = fx.put(fx.users["mum"], fx.destination_id, short, "mum.kv")
    assert done.status_code == 422
    assert done.json()["reason"] == "invalid_token"
    assert fx.accounts() == []


@pytest.mark.req("EF-USR-012", "EF-USR-017")
@pytest.mark.parametrize("name", ["~abc", "~01HZZZZZZZZZZZZZZZZZZZZZZZ"])
def test_a_name_that_starts_with_a_tilde_is_declared_by_nobody(fx: Fixture, name: str) -> None:
    kid, _ = _member(fx, "kid", "child", max_age_rating=10)
    fx.as_user(ADMIN)
    declared = fx.send("PUT", f"{PROFILES_PATH}/{kid}/kavita-account", {"kavita_username": name})
    assert declared.status_code == 422
    _member(fx, "mum")
    fx.as_user("mum")
    pasted = fx.put(fx.users["mum"], fx.destination_id, "a-key-of-enough-signs", name)
    assert pasted.status_code == 422
    assert pasted.json()["reason"] == "invalid_remote_user"


@pytest.mark.req("EF-USR-012", "EF-USR-017")
def test_a_declared_name_that_only_changes_case_keeps_the_key(fx: Fixture) -> None:
    kid, kid_key = _member(fx, "kid", "child", max_age_rating=10)
    _declare(fx, kid, "kid.kv")
    assert fx.put(kid, fx.destination_id, kid_key).status_code == 200
    _declare(fx, kid, "KID.KV")  # Kavita sees one account: the key still opens it
    assert fx.service.open_token(kid, fx.destination_id) is not None
    _declare(fx, kid, "other.kv")  # another account: the key is of no use
    assert fx.service.open_token(kid, fx.destination_id) is None


@pytest.mark.req("EF-USR-012")
def test_a_member_links_no_destination_of_a_shelf_closed_to_them(fx: Fixture) -> None:
    mum, mum_key = _member(fx, "mum")
    other, _ = _member(fx, "dad")
    shelf = fx.rows("SELECT shelf_id FROM shelf_destination WHERE id = ?", fx.destination_id)[0][0]
    with closing(sqlite3.connect(fx.settings.database_path)) as db:
        db.execute(
            "UPDATE shelf SET allowed_profiles = ? WHERE id = ?", (json.dumps([other]), shelf)
        )
        db.commit()
    fx.as_user("mum")
    closed = fx.put(mum, fx.destination_id, mum_key, "mum.kv")
    assert closed.status_code == 404  # as if the destination were not there
    assert fx.accounts() == []
    assert fx.put(mum, fx.other_destination_id, mum_key, "mum.kv").status_code == 200  # open shelf


@pytest.mark.req("EF-USR-012")
@pytest.mark.parametrize(
    ("roles", "warnings"), [(("Login",), []), (("Admin", "Login"), ["kavita_admin"])]
)
def test_an_adult_with_an_administrator_account_is_told_once(
    fx: Fixture, roles: tuple[str, ...], warnings: list[str]
) -> None:
    mum, _ = _member(fx, "mum")
    key = fx.fake.add_member_key("mum.kv", roles=roles)
    fx.as_user("mum")
    done = fx.put(mum, fx.destination_id, key, "mum.kv")
    assert done.status_code == 200, done.text
    assert done.json()["warnings"] == warnings
    listed = fx.send("GET", f"{PROFILES_PATH}/{mum}/destination-accounts").json()["items"]
    assert "warnings" not in listed[0]  # said once, at link time


@pytest.mark.req("EF-USR-012", "EF-USR-017")
@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"roles": ("Login", "Promote")}, "child_account_privileged"),
        ({"roles": ("Login", "Change Restriction")}, "child_account_privileged"),
        ({"roles": ("Login", "Surprise")}, "child_account_privileged"),
        ({"roles": ("Admin",)}, "child_account_admin"),
        ({"age_rating": None}, "child_account_unrestricted"),
        ({"age_rating": -1}, "child_account_unrestricted"),
        ({"age_rating": 12}, "child_account_restriction_too_open"),  # 18+ over a ceiling of 10
        ({"include_unknowns": True}, "child_account_restriction_too_open"),
    ],
)
def test_a_child_takes_only_the_key_of_an_account_a_child_may_hold(
    fx: Fixture, kwargs: dict[str, Any], reason: str
) -> None:
    kid, _ = _member(fx, "kid", "child", max_age_rating=10)
    key = fx.fake.add_member_key("kid.kv", **kwargs)
    _declare(fx, kid, "kid.kv")
    done = fx.put(kid, fx.destination_id, key)
    assert done.status_code == 422, done.text
    body = done.json()
    assert body["reason"] == reason
    assert body["type"] == "/api/v1/problems/destination-account-child-refused"
    assert fx.accounts() == []
    assert fx.service.open_token(kid, fx.destination_id) is None
    assert key not in done.text


@pytest.mark.req("EF-USR-012", "EF-USR-017")
def test_a_child_takes_the_key_of_a_restricted_reader(fx: Fixture) -> None:
    kid, _ = _member(fx, "kid", "child", max_age_rating=10)
    key = fx.fake.add_member_key("kid.kv", roles=("Login", "Read Only"), age_rating=5)
    _declare(fx, kid, "kid.kv")
    assert fx.put(kid, fx.destination_id, key).status_code == 200
