"""M6 L13: the Tracker contract's host — one link per profile, opt-in push, a pull that is a
hint (EF-INT-010, D18, EF-USR-024).

Each profile links the tracker of their own remote account: the token is in the request body
once, sealed to the `tracker_link` row, and injected by the Fetcher when the host calls the
plugin — the plugin never sees it. A child profile is refused by default; the admin may allow
one tracker to one child profile, as a reserved action. Nothing leaves before the profile has
said yes to the push, and a pull never writes a read mark: it comes back as a `TrackerHint`.

Every token below is drawn at random by the test: no secret is a literal.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.domain.catalogue import RESERVED_ACTIONS
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.sdk.testing.fakes import TRACKER_KEY
from tests.tracker_env import REMOTE_ID, Fixture, trackers_path, tracker_env

pytestmark = pytest.mark.req("EF-INT-010", "EF-USR-024", "ENF-SEC-005")

ALLOWANCE = "/allowance"
PUSH = "/push"
PROGRESS = "/progress"
PULL = "/pull"


@pytest.fixture
def fx(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Fixture]:
    with tracker_env(monkeypatch, settings) as env:
        yield env


def _token() -> str:
    return secrets.token_urlsafe(32)


def _engine(fx: Fixture) -> Any:
    return create_db_engine(fx.settings.database_path)


def _admin_id(fx: Fixture) -> str:
    return fx.rows('SELECT id FROM "user" WHERE username = ?', "admin")[0][0]


# --- the link: per profile, sealed, injected by the Fetcher -------------------------------


def test_an_adult_links_their_own_tracker(fx: Fixture) -> None:
    fx.add("mum", "member")
    mum = fx.client_of("mum")
    token = _token()
    done = fx.link("mum", token, client=mum)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["plugin_key"] == TRACKER_KEY
    assert body["push_enabled"] is False  # nothing leaves before the profile says yes
    assert body["token_fingerprint"] and token not in body["token_fingerprint"]
    assert token not in done.text
    rows = fx.links()
    assert len(rows) == 1
    _user, plugin_key, remote_ref, push_enabled, sealed, _print = rows[0]
    assert (plugin_key, push_enabled, sealed) == (TRACKER_KEY, False, True)
    assert json.loads(remote_ref)["remote_id"] == REMOTE_ID
    assert token not in fx.written()  # sealed: not in the file in clear
    # the plugin was called, through the Fetcher that carries the credential
    assert [call[0] for call in fx.fake.calls] == ["link"]
    assert fx.fetcher.seen[0][0] == "POST https://tracker.invalid/links"
    # a second link of the same tracker replaces the token: one row, a new fingerprint
    second = _token()
    again = fx.link("mum", second, client=mum)
    assert again.status_code == 200
    assert again.json()["token_fingerprint"] != body["token_fingerprint"]
    assert len(fx.links()) == 1
    assert second not in fx.written() and token not in fx.written()


def test_the_link_is_listed_without_the_token(fx: Fixture) -> None:
    fx.add("mum", "member")
    mum = fx.client_of("mum")
    token = _token()
    assert fx.link("mum", token, client=mum).status_code == 200
    listed = fx.send("GET", trackers_path(fx.users["mum"]), client=mum)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [i["plugin_key"] for i in items] == [TRACKER_KEY]
    assert items[0]["push_enabled"] is False
    assert "token" not in listed.text and token not in listed.text


def test_unlink_removes_the_link_and_the_secret(fx: Fixture) -> None:
    fx.add("mum", "member")
    mum = fx.client_of("mum")
    assert fx.link("mum", _token(), client=mum).status_code == 200
    gone = fx.send("DELETE", trackers_path(fx.users["mum"], TRACKER_KEY), client=mum)
    assert gone.status_code == 204
    assert fx.links() == []
    # the profile's own door only: another profile's unlink answers like one that is not there
    fx.add("dad", "member")
    assert fx.link("dad", _token(), client=fx.client_of("dad")).status_code == 200
    assert fx.send(
        "DELETE", trackers_path(fx.users["dad"], TRACKER_KEY), client=mum
    ).status_code == 404
    assert len(fx.links()) == 1


# --- a child is refused by default; the admin may allow one, as a reserved action ----------


def test_trackers_per_user_forbidden_for_child_by_default(fx: Fixture) -> None:
    fx.add("kid", "child", max_age_rating=10)
    kid = fx.client_of("kid")
    refused = fx.link("kid", _token(), client=kid)
    assert refused.status_code == 403
    assert refused.json()["code"] == "tracker_child_refused"
    assert fx.links() == []  # nothing written, nothing sealed
    assert fx.fake.calls == []  # the plugin was never reached
    # the child's own list is empty and says nothing of the others
    listed = fx.send("GET", trackers_path(fx.users["kid"]), client=kid)
    assert listed.status_code == 200 and listed.json()["items"] == []


def test_child_tracker_allowance_is_reserved(fx: Fixture) -> None:
    fx.add("mum", "member")
    fx.add("kid", "child", max_age_rating=10)
    kid, mum = fx.client_of("kid"), fx.client_of("mum")
    path = trackers_path(fx.users["kid"], TRACKER_KEY, ALLOWANCE)
    # a member, the child herself, and an API key: all refused
    assert fx.send("PUT", path, {"allowed": True}, client=mum).status_code == 403
    assert fx.send("PUT", path, {"allowed": True}, client=kid).status_code == 403
    key = local_auth(_engine(fx), fx.settings).create_api_key(name="admin-key").value
    bare = fx.api.other_client("10.0.0.90")
    by_key = bare.put(path, json={"allowed": True}, headers={"Authorization": f"Bearer {key}"})
    assert by_key.status_code == 403
    assert fx.allowances() == []
    # a stale session of the admin is not enough either: the action is reserved and fresh
    fx.api.clock.advance(timedelta(hours=2))
    stale = fx.send("PUT", path, {"allowed": True})
    assert stale.status_code == 401
    assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert fx.allowances() == []
    # a fresh session of the admin allows this tracker to this child, and only that
    assert fx.api.login().status_code == 200
    allowed = fx.send("PUT", path, {"allowed": True})
    assert allowed.status_code == 200 and allowed.json() == {"allowed": True}
    assert fx.allowances() == [(fx.users["kid"], TRACKER_KEY, _admin_id(fx))]
    assert fx.link("kid", _token(), client=kid).status_code == 200
    # withdrawing the allowance takes the child's link away with it
    assert fx.send("PUT", path, {"allowed": False}).status_code == 200
    assert fx.allowances() == []
    assert fx.links() == []
    assert fx.link("kid", _token(), client=kid).status_code == 403


def test_member_cannot_link_tracker_for_another_profile(fx: Fixture) -> None:
    fx.add("mum", "member")
    fx.add("dad", "member")
    fx.add("kid", "child", max_age_rating=10)
    mum = fx.client_of("mum")
    token = _token()
    # another profile's tracker answers like one that is not there (no probing)
    for method, suffix, body in (
        ("PUT", "", {"token": token}),
        ("GET", "", None),
        ("DELETE", "", None),
        ("PUT", PUSH, {"enabled": True}),
        ("POST", PROGRESS, {"fraction": 0.5}),
        ("POST", PULL, None),
    ):
        got = fx.send(method, trackers_path(fx.users["dad"], TRACKER_KEY, suffix), body, client=mum)
        assert got.status_code == 404, (method, suffix, got.status_code)
    # the admin does not link anyone's tracker either: the link is the profile's own
    admin_refused = fx.send("PUT", trackers_path(fx.users["kid"], TRACKER_KEY), {"token": token})
    assert admin_refused.status_code == 404
    assert fx.links() == []
    assert fx.fake.calls == []


# --- the secret never reaches the plugin ---------------------------------------------------


def test_tracker_secret_never_reaches_plugin(fx: Fixture) -> None:
    fx.add("mum", "member")
    mum = fx.client_of("mum")
    token = _token()
    assert fx.link("mum", token, client=mum).status_code == 200
    # what the plugin received: the calls it saw and the session states it was handed
    plugin_side = json.dumps(
        {
            "calls": [[str(part) for part in call] for call in fx.fake.calls],
            "accounts": [account.model_dump() for account in fx.fake.accounts],
        },
        default=str,
    )
    assert token not in plugin_side
    # the fetcher carried the credential to the remote service: the host injected it
    assert any(
        method == "POST" and headers.get("Authorization") == token
        for method, _url, headers in fx.fetcher.seen
    )
    # the sealed value opens from its row, with the keyring, and only there
    with _engine(fx).connect() as connection:
        row_id = fx.rows("SELECT id FROM tracker_link")[0][0]
        opened = fx.vault.open_row(connection, "tracker_link", row_id)
    assert opened == token.encode()
    # nowhere else: not the database file, not the audit, not the answers
    assert token not in fx.written()
    audit = fx.rows("SELECT action, target FROM audit WHERE action LIKE 'tracker.%'")
    assert [action for action, _ in audit] == ["tracker.linked"]
    assert all(token not in str(target) for _, target in audit)


# --- the push leaves only after an explicit opt-in -----------------------------------------


def test_tracker_push_only_after_opt_in(fx: Fixture) -> None:
    fx.add("mum", "member")
    mum = fx.client_of("mum")
    assert fx.link("mum", _token(), client=mum).status_code == 200
    progress = trackers_path(fx.users["mum"], TRACKER_KEY, PROGRESS)
    # no opt-in yet: the push is refused and the plugin is not reached
    refused = fx.send("POST", progress, {"fraction": 0.5}, client=mum)
    assert refused.status_code == 403
    assert refused.json()["code"] == "tracker_push_refused"
    assert [call[0] for call in fx.fake.calls] == ["link"]
    # the explicit accord turns the push on, for this tracker only
    opt_in = fx.send(
        "PUT", trackers_path(fx.users["mum"], TRACKER_KEY, PUSH), {"enabled": True}, client=mum
    )
    assert opt_in.status_code == 200 and opt_in.json() == {"push_enabled": True}
    pushed = fx.send("POST", progress, {"fraction": 0.5}, client=mum)
    assert pushed.status_code == 200 and pushed.json() == {"pushed": True}
    assert ("push", REMOTE_ID, 0.5) in fx.fake.calls
    # the accord is the profile's own: another profile cannot switch it
    fx.add("dad", "member")
    assert fx.send(
        "PUT", trackers_path(fx.users["mum"], TRACKER_KEY, PUSH), {"enabled": False},
        client=fx.client_of("dad"),
    ).status_code == 404
    # and saying no again closes the door
    assert fx.send(
        "PUT", trackers_path(fx.users["mum"], TRACKER_KEY, PUSH), {"enabled": False}, client=mum
    ).json() == {"push_enabled": False}
    again = fx.send("POST", progress, {"fraction": 0.6}, client=mum)
    assert again.status_code == 403
    assert ("push", REMOTE_ID, 0.6) not in fx.fake.calls


# --- the pull is a hint, never a read mark -------------------------------------------------


def test_tracker_pull_never_marks_read(fx: Fixture) -> None:
    fx.add("mum", "member")
    mum = fx.client_of("mum")
    assert fx.link("mum", _token(), client=mum).status_code == 200
    pulled = fx.send("POST", trackers_path(fx.users["mum"], TRACKER_KEY, PULL), client=mum)
    assert pulled.status_code == 200
    assert pulled.json() == {"fraction": 0.25, "at": "2026-11-20T10:00:00Z", "hint": True}
    assert ("pull", REMOTE_ID) in fx.fake.calls
    # nothing was written anywhere a read mark would live
    for table in ("read_marker", "user_state", "reading_position"):
        assert fx.rows(f"SELECT count(*) FROM {table}")[0][0] == 0, table
    # and the hint stays a hint: pulling twice writes nothing either
    again = fx.send("POST", trackers_path(fx.users["mum"], TRACKER_KEY, PULL), client=mum)
    assert again.status_code == 200
    for table in ("read_marker", "user_state", "reading_position"):
        assert fx.rows(f"SELECT count(*) FROM {table}")[0][0] == 0, table


# --- the reserved actions are declared ------------------------------------------------------


def test_the_tracker_actions_are_declared_in_the_catalogue(fx: Fixture) -> None:
    assert "touches_secrets" in {e.value for e in RESERVED_ACTIONS["tracker.link"].effects}
    assert "parental_control" in {e.value for e in RESERVED_ACTIONS["tracker.allow_child"].effects}
    assert "moves_instance_data" in {
        e.value for e in RESERVED_ACTIONS["tracker.push_opt_in"].effects
    }
    assert "manages_credentials" in {e.value for e in RESERVED_ACTIONS["tracker.unlink"].effects}
