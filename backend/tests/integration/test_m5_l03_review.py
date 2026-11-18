"""M5 L03, relecture adverse (#362) : les décisions du porteur, tests rouges d'abord.

- MIN-A : la connexion OIDC suit l'avis de restauration comme les clés d'accès ;
- MIN-B : un admin ne lie qu'à son propre compte ou à un adulte non admin ; toute liaison est
  écrite à l'audit et visible du titulaire ;
- MIN-C : le retour d'une liaison passe par une page du même site, la liaison n'est écrite qu'après
  un POST confirmé dans la session qui a lancé le flux ;
- NIT-B : un jeton aux dates non finies ou hors plage est le refus unique, jamais une erreur ;
- détails : jeton sans `kid` parmi plusieurs clés refusé, table des flux bornée par préfixe.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.adapters.oidc_flows import MemoryOidcFlows
from shelfhold.identity.application.oidc import OidcFlow, OidcUnavailableError
from tests.auth_env import db_rows
from tests.fakes.oidc.provider import ISSUER
from tests.integration.test_m5_l03_contradicteur import _restore_pending
from tests.integration.test_m5_l03_oidc import (
    FLOW_COOKIE,
    LINK,
    LOGIN,
    OIDC,
    World,
    refused,
    world_for,
)
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-USR-004")

CONFIRM = f"{OIDC}/link/confirm"
IDENTITIES = f"{OIDC}/identities"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with world_for(monkeypatch, settings) as w:
        yield w


def _returns(world: World, client: TestClient) -> Any:
    """`client` starts a link and the provider sends its browser back."""
    started = world.send(client, "POST", LINK)
    assert started.status_code == 200, started.text
    return world.callback(client, world.idp.authorize(started.json()["authorization_url"]))


# --- MIN-A: the restore notice -----------------------------------------------------------------


def test_oidc_login_does_not_begin_while_a_restore_is_pending(world: World) -> None:
    assert world.link_admin("mum").status_code == 204
    _restore_pending(world)
    answer = world.browser().get(LOGIN, follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"] == "/login?oidc_error=1"
    assert FLOW_COOKIE not in "".join(answer.headers.get_list("set-cookie"))


def test_oidc_login_begun_before_a_restore_does_not_finish_after_it(world: World) -> None:
    assert world.link_admin("mum").status_code == 204
    browser = world.browser()
    back = world.idp.authorize(world.begin(browser))
    _restore_pending(world)
    assert refused(world.callback(browser, back))
    assert world.signed_in_as(browser) is None


def test_oidc_link_stays_open_during_a_restore_notice(world: World) -> None:
    _restore_pending(world)
    assert _returns(world, world.mum).headers["location"] == CONFIRM


# --- MIN-B: who an admin may link --------------------------------------------------------------


def test_oidc_an_admin_links_only_self_or_an_adult_member(world: World) -> None:
    add_profile(world.env, "boss", "admin", "10.0.0.9")
    boss = db_rows(world.env.settings, "SELECT id FROM user WHERE username='boss'")[0][0]
    refused_target = world.send(world.admin, "PUT", f"/api/v1/profiles/{boss}/oidc", {"sub": "b"})
    assert refused_target.status_code == 403
    assert refused_target.json()["code"] == "oidc_admin_target"
    assert world.identities() == []
    assert world.link_admin("admin", sub="mine").status_code == 204  # her own account
    assert world.link_admin("mum", sub="mums").status_code == 204  # an adult member
    assert world.link_admin("kid", sub="kids").status_code == 403  # never a child
    assert {row[0] for row in world.identities()} == {world.ids["admin"], world.ids["mum"]}


def test_oidc_every_link_is_audited_and_shown_to_its_holder(world: World) -> None:
    assert world.link_admin("mum").status_code == 204
    rows = db_rows(
        world.env.settings, "SELECT actor_id, target FROM audit WHERE action='oidc.linked'"
    )
    assert [(a, t) for a, t in rows] == [(world.ids["admin"], world.ids["mum"])]
    shown = world.mum.get(IDENTITIES)
    assert shown.status_code == 200
    (item,) = shown.json()["items"]
    assert item["issuer"] == ISSUER and item["linked_by_other"] is True
    assert "sub" not in item  # nobody reads another person's identifier back
    assert world.dad.get(IDENTITIES).json()["items"] == []  # only one's own
    assert world.browser("198.51.100.80").get(IDENTITIES).status_code == 401


def test_oidc_a_link_made_by_oneself_is_shown_as_such(world: World) -> None:
    assert _returns(world, world.mum).headers["location"] == CONFIRM
    assert world.send(world.mum, "POST", CONFIRM, {"confirm": True}).status_code == 204
    (item,) = world.mum.get(IDENTITIES).json()["items"]
    assert item["linked_by_other"] is False


# --- MIN-C: the confirmation page --------------------------------------------------------------


def test_oidc_link_waits_for_the_confirmation(world: World) -> None:
    back = _returns(world, world.mum)
    assert back.status_code == 303 and back.headers["location"] == CONFIRM
    assert world.identities() == []  # the provider's answer alone writes nothing
    page = world.mum.get(CONFIRM)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "mum" in page.text and "example" in page.text and world.idp.sub in page.text
    assert "no-store" in page.headers["cache-control"]
    assert world.identities() == []  # looking at the page writes nothing
    done = world.send(world.mum, "POST", CONFIRM, {"confirm": True})
    assert done.status_code == 204
    assert world.identities() == [(world.ids["mum"], ISSUER, world.idp.sub)]
    assert "oidc.linked" in world.actions()
    replay = world.send(world.mum, "POST", CONFIRM, {"confirm": True})
    assert replay.status_code == 409 and replay.json()["code"] == "oidc_link_expired"
    assert len(world.identities()) == 1


def test_oidc_link_can_be_declined(world: World) -> None:
    assert _returns(world, world.mum).headers["location"] == CONFIRM
    assert world.send(world.mum, "POST", CONFIRM, {"confirm": False}).status_code == 204
    assert world.identities() == []
    assert world.send(world.mum, "POST", CONFIRM, {"confirm": True}).status_code == 409


def test_oidc_link_is_written_only_by_the_session_that_started_it(world: World) -> None:
    assert _returns(world, world.mum).headers["location"] == CONFIRM
    flow = world.mum.cookies.get(FLOW_COOKIE)
    assert flow
    # another person's session, carrying the browser's flow cookie: refused, nothing written
    world.dad.cookies.set(FLOW_COOKIE, flow, domain="testserver")
    theft = world.send(world.dad, "POST", CONFIRM, {"confirm": True})
    assert theft.status_code in (403, 409) and world.identities() == []
    # the same browser with no session
    stranger = world.browser("198.51.100.81")
    stranger.cookies.set(FLOW_COOKIE, flow, domain="testserver")
    assert stranger.post(CONFIRM, json={"confirm": True}).status_code in (401, 403)
    # no CSRF token: the usual refusal
    assert world.mum.post(CONFIRM, json={"confirm": True}).status_code == 403
    assert world.identities() == []
    # and the right session still can
    assert world.send(world.mum, "POST", CONFIRM, {"confirm": True}).status_code == 204


def test_oidc_link_confirmation_asks_a_fresh_session_again(world: World) -> None:
    from datetime import timedelta

    assert _returns(world, world.mum).headers["location"] == CONFIRM
    world.env.clock.advance(timedelta(seconds=world.env.settings.fresh_auth_s + 60))
    stale = world.send(world.mum, "POST", CONFIRM, {"confirm": True})
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert world.identities() == []


def test_oidc_confirmation_page_needs_a_flow(world: World) -> None:
    for client in (world.mum, world.browser("198.51.100.82")):
        answer = client.get(CONFIRM, follow_redirects=False)
        assert answer.status_code == 303 and answer.headers["location"].endswith("oidc=refused")


def test_oidc_confirmation_page_is_escaped_and_locked_down(world: World) -> None:
    hostile = '<img src=x onerror=alert(1)>"&'
    started = world.send(world.mum, "POST", LINK).json()["authorization_url"]
    world.callback(world.mum, world.idp.authorize(started, sub=hostile))
    page = world.mum.get(CONFIRM)
    assert page.status_code == 200 and "<img" not in page.text and "&lt;img" in page.text
    csp = page.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "default-src 'none'" in csp


# --- NIT-B: the dates of the token -------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        {"auth_time": 10**20},
        {"auth_time": float("nan")},
        {"auth_time": float("inf")},
        {"exp": float("inf")},
        {"exp": float("nan")},
        {"iat": float("nan")},
        {"iat": -(10**20)},
        {"exp": 10**30},
    ],
    ids=[
        "auth-huge",
        "auth-nan",
        "auth-inf",
        "exp-inf",
        "exp-nan",
        "iat-nan",
        "iat-huge",
        "exp-huge",
    ],
)
def test_oidc_dates_that_are_not_dates_are_the_one_refusal(
    world: World, tamper: dict[str, Any]
) -> None:
    assert world.link_admin("mum").status_code == 204
    world.idp.tamper = tamper
    browser = world.browser()
    assert refused(world.sign_in(browser))
    assert world.signed_in_as(browser) is None
    world.idp.tamper = {}
    assert (
        world.sign_in(world.browser("198.51.100.83")).status_code == 303
    )  # the attempt is given back


# --- details -----------------------------------------------------------------------------------


def test_oidc_a_token_without_kid_among_several_keys_is_refused(world: World) -> None:
    assert world.link_admin("mum").status_code == 204
    world.idp.second_key = True
    world.idp.omit_kid = True
    assert refused(world.sign_in(world.browser()))
    world.idp.omit_kid = False  # naming its key, the same provider signs people in
    other = world.browser("198.51.100.84")
    assert world.sign_in(other).status_code == 303 and world.signed_in_as(other) == "mum"


def _flow(owner_ip: str = "x") -> OidcFlow:
    return OidcFlow("login", "n", "v", "b", datetime(2100, 1, 1, tzinfo=UTC))


def test_oidc_flows_are_bounded_per_prefix_and_overall() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    flows = MemoryOidcFlows(per_owner=4, per_group=6, capacity=20)
    # one owner (a /64 or an address) keeps four: its oldest goes first
    for n in range(6):
        flows.issue(
            f"s{n}", _flow(), owner="ip:2001:db8:1:1::/64", group="g:2001:db8:1::/48", now=now
        )
    assert flows.consume("s0") is None and flows.consume("s1") is None and flows.consume("s5")
    # a group (a /48 of /64s) keeps six whoever of it asks: the seventh is refused
    for n in range(10, 12):
        flows.issue(
            f"s{n}", _flow(), owner=f"ip:2001:db8:1:{n}::/64", group="g:2001:db8:1::/48", now=now
        )
    with pytest.raises(OidcUnavailableError):
        flows.issue(
            "s99", _flow(), owner="ip:2001:db8:1:99::/64", group="g:2001:db8:1::/48", now=now
        )
    # another prefix is not the first one's business
    flows.issue("t0", _flow(), owner="ip:198.51.100.1", group="g:198.51.100.1", now=now)
    # and the table as a whole has its ceiling
    for n in range(40):
        try:
            flows.issue(f"u{n}", _flow(), owner=f"ip:10.0.0.{n}", group=f"g:10.0.0.{n}", now=now)
        except OidcUnavailableError:
            break
    else:
        pytest.fail("the table has no ceiling")
