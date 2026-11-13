"""M3 L47 (EF-UX-009, EF-USR-016, EF-USR-001): the server side of the settings screens.

The screen only draws what the server answers: the level of a setting comes from the registry of
configuration keys (never from a list in the client), a field fixed by the environment is answered
locked with its origin and the server refuses to write it as well, no secret of the configuration
travels to the client, and the profile screens are the L05 routes, refused to a member, a child and
a key. Everything goes through the real application (`create_app()`, real sessions, real CSRF).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.config import Settings, load_settings
from shelfhold.headless import keys
from shelfhold.headless.keys import KEYS
from tests.auth_env import ADMIN, CSRF_HEADER, AuthEnv, auth_env, db_rows
from tests.headless_env import KAVITA_PASSWORD, Headless, headless
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.visibility_env import add_profile

CONFIG = "/api/v1/config"
PROFILES = "/api/v1/profiles"
ENV_URL = "https://env-set.example.org"
CHANNEL_TOKEN = "-".join(("synthetic", "channel", "token", "0047"))
CHANNEL_URL = f"ntfy://ntfy.example.org/{CHANNEL_TOKEN}"
NEW_PROFILE_PASSWORD = "-".join(("synthetic", "profile", "pw", "0047"))  # no literal: gitleaks
LEVELS = {"essential", "advanced", "expert"}


def _resolved_with_env(settings: Settings) -> Any:
    env = {"SHELFHOLD_DATA_DIR": str(settings.data_dir), "SHELFHOLD_PUBLIC_URL": ENV_URL}
    return lambda: load_settings(env)


@pytest.fixture
def world(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> Iterator[tuple[AuthEnv, Headless]]:
    with (
        headless(settings, resolve=_resolved_with_env(settings)) as env,
        auth_env(monkeypatch, settings, headless_admin=env.admin) as api,
    ):
        assert api.login().status_code == 200
        yield api, env


def _rows(api: AuthEnv) -> dict[str, dict[str, Any]]:
    answer = api.client.get(CONFIG)
    assert answer.status_code == 200, answer.text
    return {row["key"]: row for row in answer.json()["items"]}


def _patch(api: AuthEnv, values: dict[str, str]) -> Any:
    return api.client.patch(CONFIG, json={"values": values}, headers={CSRF_HEADER: api.csrf()})


# -- three levels -------------------------------------------------------------------------------


@pytest.mark.req("EF-UX-009")
def test_settings_three_levels_essential_max_15(world: tuple[AuthEnv, Headless]) -> None:
    api, env = world
    # the registry itself: every key has a level, at most 15 are essential (no list in the test)
    assert set(keys.LEVELS) == LEVELS
    assert {key.level for key in KEYS.values()} <= LEVELS
    assert sum(key.level == "essential" for key in KEYS.values()) <= keys.ESSENTIAL_MAX == 15
    env.admin.target_add_kavita(
        name="kavita", url="http://kavita.internal:5000", username="u", password=KAVITA_PASSWORD,
        accept_admin_risk=True, path_map=[{"local": str(env.settings.data_dir), "remote": "/m"}],
    )  # fmt: skip
    # what the screen gets: the same levels, for the registry and for the rows made per target
    rows = _rows(api)
    assert set(KEYS) <= set(rows)
    assert any(key.startswith(keys.PATH_MAP_PREFIX) for key in rows)
    assert {row["level"] for row in rows.values()} <= LEVELS
    assert all(rows[name]["level"] == key.level for name, key in KEYS.items())
    assert sum(row["level"] == "essential" for row in rows.values()) <= 15


@pytest.mark.req("EF-UX-009")
def test_a_sixteenth_essential_setting_or_a_missing_level_is_refused() -> None:
    def key(name: str, level: str) -> keys.ConfigKey:
        return keys.ConfigKey(name, None, "help", level=level)

    ok = {f"k{i}": key(f"k{i}", "essential") for i in range(15)}
    keys.check_levels(ok)  # fifteen is the limit
    too_many = {**ok, "k15": key("k15", "essential")}
    with pytest.raises(ValueError, match="essential"):
        keys.check_levels(too_many)
    with pytest.raises(ValueError, match="level"):
        keys.check_levels({"x": key("x", "secret-corner")})


# -- fixed by the environment: greyed with its origin, and refused by the server too ------------


@pytest.mark.req("EF-UX-009")
def test_env_set_field_greyed_with_origin(world: tuple[AuthEnv, Headless]) -> None:
    api, _env = world
    rows = _rows(api)
    assert rows["public_url"]["locked"] is True
    assert rows["public_url"]["overridden_by"] == "env"
    assert rows["maintenance.window"]["locked"] is True  # fixed in this release: greyed as well
    assert rows["maintenance.window"]["source"] == "fixed"
    assert rows["source.check_mode"]["locked"] is False
    assert rows["source.check_mode"]["overridden_by"] is None


@pytest.mark.req("EF-UX-009", "ENF-OPS-004")
def test_server_refuses_to_write_a_field_the_environment_sets(
    world: tuple[AuthEnv, Headless],
) -> None:
    api, env = world
    before = {k: r["value"] for k, r in _rows(api).items()}
    refused = _patch(api, {"public_url": "https://other.example.org"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "config.key_readonly"
    # one locked key in a batch: none of the batch is written (no half-applied form)
    mixed = _patch(api, {"source.check_mode": "piloted", "public_url": "https://o.example.org"})
    assert mixed.status_code == 409
    assert {k: r["value"] for k, r in _rows(api).items()} == before
    assert env.admin.get("source.check_mode").source == "default"
    # a free field of the same screen still saves
    saved = _patch(api, {"source.check_mode": "piloted"})
    assert saved.status_code == 200
    assert {r["key"]: r for r in saved.json()["items"]}["source.check_mode"]["value"] == "piloted"
    # the fixed key of this release is refused as before
    assert _patch(api, {"maintenance.window": "01:00-02:00 UTC"}).status_code == 409


# -- no secret reaches the client ----------------------------------------------------------------


@pytest.mark.req("EF-UX-009", "ENF-SEC-008")
def test_no_secret_of_the_configuration_is_answered_even_masked(
    world: tuple[AuthEnv, Headless],
) -> None:
    api, env = world
    env.admin.set("notify.urls", f"ops={CHANNEL_URL}")
    answers = [api.client.get(CONFIG), _patch(api, {"source.check_mode": "always_on"})]
    for answer in answers:
        assert answer.status_code == 200
        assert CHANNEL_TOKEN not in answer.text and "ntfy.example.org" not in answer.text
        assert "ntfy://" not in answer.text and "*" not in answer.text
        row = {r["key"]: r for r in answer.json()["items"]}["notify.urls"]
        assert row["secret"] is True and row["value"] is None
        assert {
            r["secret"]
            for k, r in {r["key"]: r for r in answer.json()["items"]}.items()
            if k != "notify.urls"
        } == {False}
    # and it cannot be written from this screen: the CLI or the file only
    denied = _patch(api, {"notify.urls": f"x={CHANNEL_URL}"})
    assert denied.status_code == 403 and denied.json()["code"] == "credential_not_allowed"


# -- who may read and write the settings ---------------------------------------------------------


@pytest.mark.req("EF-UX-009", "ENF-SEC-010")
def test_settings_are_refused_to_member_child_and_nobody(
    world: tuple[AuthEnv, Headless],
) -> None:
    api, _env = world
    clients = {
        "member": add_profile(api, "mum", "member", "10.0.0.7"),
        "child": add_profile(api, "kid", "child", "10.0.0.8", max_age_rating=10),
    }
    for name, client in clients.items():
        assert client.get(CONFIG).status_code == 403, name
        headers = {CSRF_HEADER: api.csrf(client)}
        patched = client.patch(CONFIG, json={"values": {"source.check_mode": "piloted"}},
                               headers=headers)  # fmt: skip
        assert patched.status_code == 403, name
    anonymous = api.other_client("10.0.0.9")
    assert anonymous.get(CONFIG).status_code == 401
    assert api.client.get(CONFIG).status_code == 200  # the admin still reads it
    assert _rows(api)["source.check_mode"]["value"] == "always_on"  # nothing was written


# -- the profile screens: admin only, fresh session, never a key ---------------------------------


@pytest.mark.req("EF-USR-001", "EF-USR-022", "ENF-SEC-010")
def test_profile_screen_requires_reauth(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    world: tuple[AuthEnv, Headless],
) -> None:
    api, _env = world
    key = _create_key(monkeypatch, capsys, api.settings)
    assert re.fullmatch(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}", key)
    member = add_profile(api, "mum", "member", "10.0.0.7")
    child = add_profile(api, "kid", "child", "10.0.0.8", max_age_rating=10)
    kid_id = str(db_rows(api.settings, 'SELECT id FROM "user" WHERE username = ?', "kid")[0][0])
    reads = (
        ("GET", PROFILES, None),
        ("GET", f"{PROFILES}/{kid_id}/source-policies", None),
    )
    writes = (
        ("POST", PROFILES, {"username": "x", "password": NEW_PROFILE_PASSWORD, "role": "member"}),
        ("PUT", f"{PROFILES}/{kid_id}/visibility", {"visibility": "household"}),
        ("PATCH", f"{PROFILES}/{kid_id}", {"max_age_rating": 16}),
    )
    for who, client in (("member", member), ("child", child)):
        for method, path, body in (*reads, *writes):
            headers = {CSRF_HEADER: api.csrf(client)}
            answer = client.request(method, path, headers=headers, json=body)
            assert answer.status_code == 403, (who, method, path)
    for method, path, body in (*reads, *writes):
        answer = api.client.request(method, path, headers=_bearer(key), json=body)
        assert answer.status_code in {401, 403}, ("key", method, path)
        assert "kid" not in answer.text or answer.status_code == 403
    assert api.client.get(PROFILES).status_code == 200
    api.clock.advance(timedelta(minutes=20))  # the session is no longer fresh
    for method, path, body in writes:
        stale = api.client.request(method, path, headers={CSRF_HEADER: api.csrf()}, json=body)
        assert stale.status_code == 401, (method, path)
        assert stale.json()["type"] == "/api/v1/problems/reauth-required"
        assert "password" in stale.json()["reauth_methods"]  # what the screen asks for in place
    names = {r[0] for r in db_rows(api.settings, 'SELECT username FROM "user"')}
    assert names == {ADMIN, "mum", "kid"}  # nothing was created by any refused call
