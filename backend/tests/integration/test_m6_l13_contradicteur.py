"""M6 L13, relecture adverse: what the tracker host still lets through (EF-INT-010, D18).

Each test is red on the head of the lot and names the rule it holds:

- a profile demoted to child keeps the tracker link (and the push) it made as an adult;
- the admin's withdrawal that lands while a child's link is in flight leaves the link behind;
- the Fetcher handed to the plugin gives the token back to it (`ref_secret`, `secret_values`);
- the sealed token follows the plugin key, not the origin it was linked for: a plugin that
  declares another `api` domain under the same key receives it.

Every token below is drawn at random by the test: no secret is a literal.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, suppress
from typing import Any

import pytest
from shelfhold_sdk.models import AuthApiKey, DomainRule, Permissions
from shelfhold_sdk.testing.fakes import TRACKER_KEY

from shelfhold.config import Settings
from tests.tracker_env import Fixture, tracker_env, trackers_path

pytestmark = pytest.mark.req("EF-INT-010", "EF-USR-024", "ENF-SEC-005")

PROFILES = "/api/v1/profiles"


@pytest.fixture
def fx(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Fixture]:
    with tracker_env(monkeypatch, settings) as env:
        yield env


def _token() -> str:
    return secrets.token_urlsafe(32)


def test_a_profile_demoted_to_child_keeps_no_tracker_link(fx: Fixture) -> None:
    """A child is refused a tracker unless the admin allowed it: the role change must not keep
    the link (and its push) an adult made — the OIDC link is dropped the same way (M5 L03)."""
    fx.add("mum", "member")
    mum = fx.client_of("mum")
    assert fx.link("mum", _token(), client=mum).status_code == 200
    push = trackers_path(fx.users["mum"], TRACKER_KEY, "/push")
    assert fx.send("PUT", push, {"enabled": True}, client=mum).status_code == 200
    # the admin's fresh session turns the profile into a child, with no allowance given
    changed = fx.send(
        "PATCH", f"{PROFILES}/{fx.users['mum']}", {"role": "child", "max_age_rating": 10}
    )
    assert changed.status_code == 200, changed.text
    assert fx.allowances() == []
    # the child's link is gone, and with it the push and the pull
    assert fx.links() == []
    now_kid = fx.client_of("mum")
    pulled = fx.send("POST", trackers_path(fx.users["mum"], TRACKER_KEY, "/pull"), client=now_kid)
    assert pulled.status_code in (403, 404), pulled.status_code
    pushed = fx.send(
        "POST",
        trackers_path(fx.users["mum"], TRACKER_KEY, "/progress"),
        {"fraction": 0.5},
        client=now_kid,
    )
    assert pushed.status_code in (403, 404), pushed.status_code
    assert not any(call[0] == "push" for call in fx.fake.calls)


def test_an_allowance_withdrawn_while_the_child_links_leaves_no_link(fx: Fixture) -> None:
    """The allowance is read before the plugin's network call and never again: a withdrawal
    committed meanwhile (it deletes the allowance and the link, which is not written yet) is
    undone by the link the host writes afterwards."""
    fx.add("kid", "child", max_age_rating=10)
    kid = fx.client_of("kid")
    allowance = trackers_path(fx.users["kid"], TRACKER_KEY, "/allowance")
    assert fx.send("PUT", allowance, {"allowed": True}).status_code == 200
    original = fx.fake.link

    async def link_while_the_admin_withdraws(f: Any, account: Any, ids: Any) -> Any:
        ref = await original(f, account, ids)
        # the admin's withdrawal commits while the remote service answers
        with closing(sqlite3.connect(fx.settings.database_path)) as db:
            db.execute("DELETE FROM tracker_allowance")
            db.execute("DELETE FROM tracker_link")
            db.commit()
        return ref

    fx.fake.link = link_while_the_admin_withdraws  # type: ignore[method-assign]
    fx.link("kid", _token(), client=kid)
    assert fx.allowances() == []
    assert fx.links() == []  # no allowance, no link: whatever the order of the two writes


def test_the_fetcher_handed_to_a_tracker_gives_the_token_back_to_nobody(fx: Fixture) -> None:
    """`AuthInjectingFetcher.ref_secret()` and `secret_values()` are public methods of the very
    object the plugin is handed: a tracker manifest that declares `ref_template` reads the
    token back in clear (the card: injected by the Fetcher, never given to the plugin)."""
    fx.add("mum", "member")
    mum = fx.client_of("mum")
    fx.fake.manifest = fx.fake.manifest.model_copy(
        update={
            "auth": AuthApiKey(location="header", name="Authorization", ref_template="{secret}")
        }
    )
    original = fx.fake.link
    grabbed: list[str] = []

    async def curious_link(f: Any, account: Any, ids: Any) -> Any:
        for name in ("ref_secret", "secret_values"):
            method = getattr(f, name, None)
            if method is not None:
                grabbed.append(repr(method()))
        return await original(f, account, ids)

    fx.fake.link = curious_link  # type: ignore[method-assign]
    token = _token()
    fx.link("mum", token, client=mum)
    assert all(token not in value for value in grabbed), "the plugin read the token back"


def test_the_token_does_not_follow_the_key_to_another_origin(fx: Fixture) -> None:
    """The link was made for the origin the plugin declared at link time. A plugin installed
    again under the same key (an update, a replacement) that declares another `api` domain
    must not receive the sealed token there without the profile linking again."""
    fx.add("mum", "member")
    mum = fx.client_of("mum")
    token = _token()
    assert fx.link("mum", token, client=mum).status_code == 200
    elsewhere = "https://elsewhere.invalid"
    fx.fake.manifest = fx.fake.manifest.model_copy(
        update={
            "permissions": Permissions(
                domains=[DomainRule(pattern="elsewhere.invalid", purpose="api")]
            )
        }
    )

    async def pull_elsewhere(f: Any, ref: Any) -> Any:
        with suppress(Exception):  # the unscripted answer does not matter here
            await f.get(f"{elsewhere}/progress/{ref.remote_id}")
        return None

    fx.fake.pull = pull_elsewhere  # type: ignore[method-assign]
    fx.send("POST", trackers_path(fx.users["mum"], TRACKER_KEY, "/pull"), client=mum)
    leaked = [
        url
        for _method, url, headers in fx.fetcher.seen
        if url.startswith(elsewhere) and token in headers.values()
    ]
    assert leaked == [], "the sealed token went to an origin the profile never linked"
