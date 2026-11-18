"""M5 L04: a session opened by the provider is fresh only on proof (EF-USR-024, EF-USR-004).

A session opened by OpenID Connect is never fresh (L03). To do a reserved action it sends the
person back to the provider with `prompt=login` and `max_age`, and becomes fresh only when the ID
token that comes back carries an `auth_time` inside `max_age`, for the very identity the session
was opened for. Unlinking an identity is itself a reserved action, tells its holder, and
`restore acknowledge` lists the links to check. The provider is the in-process one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.restore_notice import RestoreNoticeData, write_notice
from shelfhold.infrastructure.schema import identity
from shelfhold.ops.restore import RestoreCredentialsExit
from tests.auth_env import CSRF_HEADER, SESSION_COOKIE, db_rows
from tests.fakes.oidc.provider import ISSUER
from tests.integration.test_m5_l03_oidc import (
    FLOW_COOKIE,
    LINK,
    LOGIN,
    OIDC,
    SESSION,
    World,
    world_for,
)

pytestmark = pytest.mark.req("EF-USR-024")

REAUTH = f"{OIDC}/reauth"
IDENTITIES = f"{OIDC}/identities"
SETTINGS_SCREEN = "/reglages/compte/oidc"
HOST = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with world_for(monkeypatch, settings) as w:
        yield w


def signed_in(world: World, name: str = "mum", ip: str = "198.51.100.30") -> TestClient:
    """A person of the provider, signed in: a session that is not fresh."""
    assert world.link_admin(name).status_code == 204
    browser = world.browser(ip)
    assert world.sign_in(browser).status_code == 303
    assert world.signed_in_as(browser) == name
    return browser


def reauth_url(world: World, browser: TestClient) -> str:
    answer = world.send(browser, "POST", REAUTH)
    assert answer.status_code == 200, answer.text
    return str(answer.json()["authorization_url"])


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def is_fresh(world: World, browser: TestClient) -> bool:
    """`oidc.link_own` is reserved and asks a fresh session: 200 when it is, 401 when it is not."""
    answer = world.send(browser, "POST", LINK)
    assert answer.status_code in (200, 401), answer.text
    return answer.status_code == 200


# --- the reserved action asks the provider ----------------------------------------------------


def test_oidc_reserved_action_requires_max_age(world: World) -> None:
    mum = signed_in(world)
    refused = world.send(mum, "POST", LINK)
    assert refused.status_code == 401 and refused.json()["code"] == "reauth_required"
    assert refused.json()["reauth_methods"][0] == "oidc"  # the way the session was opened first
    assert "password" in refused.json()["reauth_methods"]  # and the local ones stay possible

    url = reauth_url(world, mum)
    asked = query(url)
    assert asked["prompt"] == "login" and asked["max_age"] == "300"  # 300 s unless set otherwise
    assert asked["code_challenge_method"] == "S256" and asked["nonce"] and asked["state"]
    assert FLOW_COOKIE in mum.cookies

    back = world.callback(mum, world.idp.authorize(url))
    assert back.status_code == 303 and back.headers["location"] == f"{SETTINGS_SCREEN}?oidc=reauth"
    assert is_fresh(world, mum)  # the same browser, now fresh, whatever the old token was
    assert "auth.reauth" in world.actions()
    assert world.signed_in_as(mum) == "mum"


def test_oidc_max_age_is_the_one_the_admin_set(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with world_for(monkeypatch, settings, oidc_max_age_s=120) as w:
        browser = signed_in(w)
        assert query(reauth_url(w, browser))["max_age"] == "120"


@pytest.mark.parametrize("value", [0, 29, 901])
def test_oidc_max_age_has_bounds(settings: Settings, value: int) -> None:
    with pytest.raises(ValueError, match="oidc_max_age_s"):
        Settings.model_validate({**settings.model_dump(), "oidc_max_age_s": value})


def test_oidc_missing_auth_time_refused(world: World) -> None:
    mum = signed_in(world)
    world.idp.auth_time_age_s = None  # the provider says nothing of when the person signed in
    url = reauth_url(world, mum)
    back = world.callback(mum, world.idp.authorize(url))
    assert back.status_code == 303 and back.headers["location"] == f"{SETTINGS_SCREEN}?oidc=refused"
    assert not is_fresh(world, mum)  # nothing was believed
    assert "auth.reauth" not in world.actions()


@pytest.mark.parametrize(
    "age",
    [301, 3600, -600],
    ids=["just-over-max-age", "an-hour-old", "in-the-future"],
)
def test_oidc_an_auth_time_outside_max_age_is_refused(world: World, age: int) -> None:
    mum = signed_in(world)
    world.idp.honours_prompt = False  # a provider that answered from its own session
    world.idp.auth_time_age_s = age
    back = world.callback(mum, world.idp.authorize(reauth_url(world, mum)))
    assert back.headers["location"] == f"{SETTINGS_SCREEN}?oidc=refused"
    assert not is_fresh(world, mum)


def test_oidc_an_auth_time_inside_max_age_is_accepted_and_is_the_freshness(world: World) -> None:
    mum = signed_in(world)
    world.idp.honours_prompt = False
    world.idp.auth_time_age_s = 299
    back = world.callback(mum, world.idp.authorize(reauth_url(world, mum)))
    assert back.headers["location"].endswith("?oidc=reauth")
    # the session is as fresh as the provider's own sign-in, not fresher
    (auth_time,) = db_rows(world.env.settings, "SELECT auth_time FROM session WHERE user_id=?",
                           world.ids["mum"])[-1:]
    assert auth_time[0] is not None
    world.env.clock.advance(timedelta(seconds=700))  # 299 + 700 s: past the 900 s allowed
    assert not is_fresh(world, mum)


def test_oidc_reauth_by_another_person_of_the_provider_is_refused(world: World) -> None:
    """Whoever signs in at the provider must be the identity this session was opened for."""
    mum = signed_in(world)
    other = world.idp.authorize(reauth_url(world, mum), sub="somebody-else")
    back = world.callback(mum, other)
    assert back.headers["location"] == f"{SETTINGS_SCREEN}?oidc=refused"
    assert not is_fresh(world, mum)


def test_oidc_reauth_flow_is_bound_to_the_browser_and_single_use(world: World) -> None:
    mum = signed_in(world)
    url = world.idp.authorize(reauth_url(world, mum))
    thief = world.browser("198.51.100.99")
    assert world.callback(thief, url).headers["location"].startswith("/login")  # no flow cookie
    assert not is_fresh(world, mum)
    assert world.callback(mum, url).headers["location"].endswith("?oidc=reauth")
    assert world.callback(mum, url).headers["location"].startswith("/login")  # used up


def test_oidc_reauth_is_for_the_session_the_provider_opened(world: World) -> None:
    """A session opened with a password stays fresh by the password: the provider's word is not
    what makes it fresh (a compromised provider account would otherwise open every action)."""
    world.link_admin("mum")
    answer = world.send(world.mum, "POST", REAUTH)
    assert answer.status_code == 403 and answer.json()["code"] == "forbidden"
    assert world.send(world.browser("198.51.100.31"), "POST", REAUTH).status_code == 401


def test_oidc_reauth_is_refused_to_a_key_and_to_a_device_token(world: World) -> None:
    key = world.send(world.admin, "POST", "/api/v1/api-keys", {"name": "k", "scopes": ["read"]})
    assert key.status_code in (200, 201), key.text
    bare = world.browser("198.51.100.32")
    answer = bare.post(REAUTH, headers={"Authorization": f"Bearer {key.json()['key']}"})
    assert answer.status_code in (401, 403)


def test_oidc_reauth_after_the_link_was_removed_is_refused(world: World) -> None:
    mum = signed_in(world)
    url = world.idp.authorize(reauth_url(world, mum))
    assert world.send(world.admin, "DELETE", f"/api/v1/profiles/{world.ids['mum']}/oidc").status_code == 204
    back = world.callback(mum, url)
    assert not back.headers["location"].endswith("?oidc=reauth")
    assert world.signed_in_as(mum) is None  # the link was the session's ground


def test_oidc_reauth_during_a_restore_notice_is_refused(world: World) -> None:
    mum = signed_in(world)
    url = world.idp.authorize(reauth_url(world, mum))
    engine = create_db_engine(world.env.settings.database_path)
    try:
        with write_transaction(engine) as connection:
            write_notice(
                connection, RestoreNoticeData(world.env.clock.now().isoformat(), None, 0, 0),
                keep_existing=False,
            )
    finally:
        engine.dispose()
    back = world.callback(mum, url)
    assert not back.headers["location"].endswith("?oidc=reauth")


# --- unlinking ---------------------------------------------------------------------------------


def test_oidc_unlink_is_reserved(world: World) -> None:
    mum = signed_in(world)
    refused = world.send(mum, "DELETE", IDENTITIES)
    assert refused.status_code == 401 and refused.json()["code"] == "reauth_required"
    assert len(world.identities()) == 1  # nothing was removed

    back = world.callback(mum, world.idp.authorize(reauth_url(world, mum)))
    assert back.headers["location"].endswith("?oidc=reauth")
    done = world.send(mum, "DELETE", IDENTITIES)
    assert done.status_code == 204
    assert world.identities() == []
    assert "oidc.unlinked" in world.actions()
    assert world.signed_in_as(mum) is None  # the sessions the link opened go with it
    assert world.sign_in(world.browser("198.51.100.33")).headers["location"].startswith("/login")


def test_oidc_unlink_with_a_password_session_needs_a_fresh_one_too(world: World) -> None:
    world.link_admin("mum")
    world.env.clock.advance(timedelta(seconds=1000))  # the password entry is old now
    stale = world.send(world.mum, "DELETE", IDENTITIES)
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert len(world.identities()) == 1
    assert "oidc" not in stale.json()["reauth_methods"]  # not a session of the provider


def test_oidc_unlink_of_nothing_is_a_plain_answer(world: World) -> None:
    assert world.send(world.mum, "DELETE", IDENTITIES).status_code == 404


def test_oidc_unlink_attacks(world: World) -> None:
    world.link_admin("mum")
    # IDOR: a member cannot unlink another account, and the admin route is the admin's alone
    assert world.send(world.dad, "DELETE", f"/api/v1/profiles/{world.ids['mum']}/oidc").status_code == 403
    assert world.send(world.kid, "DELETE", f"/api/v1/profiles/{world.ids['mum']}/oidc").status_code == 403
    assert world.send(world.kid, "DELETE", IDENTITIES).status_code == 403  # a child has none
    assert world.browser("198.51.100.34").delete(IDENTITIES).status_code == 401
    # an API key, whatever its scope
    key = world.send(world.admin, "POST", "/api/v1/api-keys", {"name": "k", "scopes": ["admin"]})
    bearer = {"Authorization": f"Bearer {key.json()['key']}"}
    bare = world.browser("198.51.100.35")
    assert bare.delete(f"/api/v1/profiles/{world.ids['mum']}/oidc", headers=bearer).status_code == 403
    assert len(world.identities()) == 1


def test_oidc_unlink_without_the_csrf_token_is_refused(world: World) -> None:
    mum = signed_in(world)
    world.callback(mum, world.idp.authorize(reauth_url(world, mum)))
    assert mum.delete(IDENTITIES).status_code in (401, 403)  # no CSRF header
    assert len(world.identities()) == 1


def test_oidc_an_admin_unlinks_another_account_and_the_holder_is_told(world: World) -> None:
    world.link_admin("mum")
    done = world.send(world.admin, "DELETE", f"/api/v1/profiles/{world.ids['mum']}/oidc")
    assert done.status_code == 204 and world.identities() == []
    rows = db_rows(
        world.env.settings, "SELECT actor_id, target FROM audit WHERE action='oidc.unlinked'"
    )
    assert [(a, t) for a, t in rows] == [(world.ids["admin"], world.ids["mum"])]
    assert notices(world, "mum") == ["identity_unlinked"]
    assert world.send(world.admin, "DELETE", f"/api/v1/profiles/{world.ids['mum']}/oidc").status_code == 404


def test_oidc_an_admin_cannot_unlink_another_admin(world: World) -> None:
    from tests.visibility_env import add_profile

    add_profile(world.env, "boss", "admin", "10.0.0.9")
    boss = db_rows(world.env.settings, "SELECT id FROM user WHERE username='boss'")[0][0]
    assert world.send(world.admin, "DELETE", f"/api/v1/profiles/{boss}/oidc").status_code in (403, 404)


# --- telling the holder ------------------------------------------------------------------------


def notices(world: World, name: str) -> list[str]:
    """The security notices queued for a person, by what they say (never a claim)."""
    engine = create_db_engine(world.env.settings.database_path)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sa.select(identity.notification_digest.c.payload).where(
                    identity.notification_digest.c.user_id == world.ids[name]
                )
            ).scalars()
            return [
                item["what"]
                for payload in rows
                for item in payload["items"]
                if item["kind"] == "security"
            ]
    finally:
        engine.dispose()


def test_oidc_the_holder_is_told_when_an_admin_links_their_account(world: World) -> None:
    world.link_admin("mum")
    assert notices(world, "mum") == ["identity_linked_by_admin"]
    assert notices(world, "admin") == []
    world.link_admin("admin", sub="mine")  # one's own link by oneself is not news
    assert notices(world, "admin") == []


def test_oidc_the_notice_goes_through_the_digest_and_names_no_identifier(world: World) -> None:
    world.link_admin("mum", sub="the-subject-of-mum")
    from tests.auth_env import db_dump

    assert "the-subject-of-mum" not in " ".join(
        str(p) for (p,) in db_rows(world.env.settings, "SELECT payload FROM notification_digest")
    )
    assert "the-subject-of-mum" in db_dump(world.env.settings)  # only the link itself keeps it


# --- restore acknowledge -----------------------------------------------------------------------


def test_oidc_restore_acknowledge_lists_the_links_to_check(world: World) -> None:
    world.link_admin("mum", sub="the-subject-of-mum")
    engine = create_db_engine(world.env.settings.database_path)
    try:
        with write_transaction(engine) as connection:
            write_notice(
                connection, RestoreNoticeData(world.env.clock.now().isoformat(), None, 0, 0),
                keep_existing=False,
            )
        listed = RestoreCredentialsExit(engine, world.env.clock).acknowledge(principal=HOST)
    finally:
        engine.dispose()
    links = [line for line in listed if line.startswith("oidc: ")]
    assert len(links) == 1 and "mum" in links[0] and ISSUER in links[0]
    assert "the-subject-of-mum" not in " ".join(listed)  # the identifier is not printed


def test_oidc_restore_waiting_item_names_the_links(world: World) -> None:
    engine = create_db_engine(world.env.settings.database_path)
    try:
        with write_transaction(engine) as connection:
            write_notice(
                connection, RestoreNoticeData(world.env.clock.now().isoformat(), None, 0, 0),
                keep_existing=False,
            )
        (item,) = RestoreCredentialsExit(engine, world.env.clock).waiting()
    finally:
        engine.dispose()
    assert "identity-provider" in item.reason and "acknowledge" in item.reason


# --- the budget of options ---------------------------------------------------------------------


def test_oidc_login_budget_is_not_the_one_of_passkey_options(world: World) -> None:
    """Thirty passkey options a minute per address were shared with the provider's start (L03
    review): a script spending one budget must not close the other, in either direction."""
    options = "/api/v1/auth/passkeys/login/options"
    one = world.browser("198.51.100.40")
    for _ in range(40):
        one.post(options, headers={CSRF_HEADER: "x"})
    assert one.get(LOGIN, follow_redirects=False).status_code == 302  # passkeys spent, OIDC not
    other = world.browser("198.51.100.41")
    for _ in range(30):
        assert other.get(LOGIN, follow_redirects=False).status_code == 302
    assert other.get(LOGIN, follow_redirects=False).status_code == 429  # its own budget is finite
    assert other.post(options, headers={CSRF_HEADER: "x"}).status_code != 429  # and only its own


# --- where a link comes back to ----------------------------------------------------------------


def test_oidc_the_result_of_a_link_is_shown_on_the_settings_screen(world: World) -> None:
    started = world.send(world.mum, "POST", LINK)
    back = world.callback(world.mum, world.idp.authorize(started.json()["authorization_url"]))
    assert back.headers["location"] == f"{OIDC}/link/confirm"
    page = world.mum.get(back.headers["location"])
    assert f"{SETTINGS_SCREEN}?oidc=linked" in page.text
    refused = world.callback(world.mum, "/api/v1/auth/oidc/callback?state=x&code=y")
    assert refused.status_code in (303, 429)


def test_oidc_no_session_cookie_is_set_by_a_refused_reauth(world: World) -> None:
    mum = signed_in(world)
    world.idp.auth_time_age_s = None
    back = world.callback(mum, world.idp.authorize(reauth_url(world, mum)))
    assert SESSION_COOKIE not in "".join(back.headers.get_list("set-cookie"))


def _unused(*_: Any) -> None:  # keeps the shared helpers imported for the type checker
    assert CSRF_HEADER and SESSION
