"""M4 L12, relecture adverse de #281 : les trous trouvés, en rouge (xfail strict).

Chaque test décrit ce qui doit tenir. Il passe au vert quand le trou est bouché : retirer alors
le marqueur `xfail`. Aucune clé n'est littérale : le Kavita simulé les tire au hasard.
"""

from collections.abc import Iterator
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
@pytest.mark.xfail(
    strict=True,
    reason="#281 contradicteur B1 : une clé refusée entre au registre des secrets connus",
)
def test_a_refused_paste_does_not_poison_the_known_secrets(fx: Fixture) -> None:
    """`MemberKey(token)` appelle `register_secret_value` avant toute vérification : n'importe
    quelle chaîne d'au moins 8 signes collée par un membre devient un « secret connu » pour toute
    la vie du processus. Le garde d'écriture refuse alors toute écriture qui la contient (ici la
    ligne d'audit d'un autre membre), et le nettoyeur la masque dans tous les journaux."""
    _member(fx, "eve")
    _mum, mum_key = _member(fx, "mum")
    fx.as_user("eve")
    # pas une clé : le nom d'une action d'audit, que Kavita refuse
    poison = fx.put(fx.users["eve"], fx.destination_id, "destination_account.linked", "eve.kv")
    assert poison.status_code == 422
    fx.as_user("mum")
    done = fx.put(fx.users["mum"], fx.destination_id, mum_key, "mum.kv")
    assert done.status_code == 200, done.text  # aujourd'hui : SecretInWriteError sur `audit`
    assert "destination_account.linked" not in shelfhold_logging.known_secret_values()


@pytest.mark.req("EF-USR-012", "EF-USR-017")
@pytest.mark.xfail(
    strict=True,
    reason="#281 contradicteur M1 : un changement de rôle garde les clés Kavita liées",
)
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
@pytest.mark.xfail(
    strict=True,
    reason="#281 contradicteur M2 : les rôles renvoyés par GET /api/Account ne sont pas lus",
)
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
