"""M4 L39: `/api/v1/me/notification-destination`, the Apprise destination of one adult profile.

Sealed with the vault (ENF-SEC-005): the URL is in the request body once and nowhere else; the
answers say only whether one is set. A session of an adult sets their own: never a child, never an
API key, never another profile's (no identifier in the path or the body), and only fresh (it is
handing over a secret). No URL in the database file in clear.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.domain.catalogue import RESERVED_ACTIONS
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env
from tests.notifications_env import CHANNEL_URL, TOKEN
from tests.visibility_env import add_profile

PATH = "/api/v1/me/notification-destination"
pytestmark = pytest.mark.req("EF-INT-021", "ENF-SEC-005", "ENF-SEC-010", "EF-USR-024")


class Rig:
    def __init__(self, env: AuthEnv, mum: TestClient, kid: TestClient) -> None:
        self.env, self.mum, self.kid = env, mum, kid

    def put(self, client: TestClient, body: Any) -> Any:
        return client.put(PATH, json=body, headers={CSRF_HEADER: self.env.csrf(client)})

    def delete(self, client: TestClient) -> Any:
        return client.delete(PATH, headers={CSRF_HEADER: self.env.csrf(client)})

    def engine(self) -> sa.Engine:
        return create_db_engine(self.env.settings.database_path)

    def stored(self) -> str:
        """Everything the database file holds, in clear or not."""
        raw = Path(self.env.settings.database_path).read_bytes().decode("latin-1")
        wal = Path(f"{self.env.settings.database_path}-wal")
        return raw + (wal.read_bytes().decode("latin-1") if wal.exists() else "")


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Rig]:
    with auth_env(monkeypatch, settings) as env:
        assert env.login().status_code == 200  # the admin
        mum = add_profile(env, "mum", "member", "10.0.0.3")
        kid = add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
        yield Rig(env, mum, kid)


def test_an_adult_seals_a_destination_of_their_own(rig: Rig) -> None:
    assert rig.mum.get(PATH).json() == {"configured": False}
    done = rig.put(rig.mum, {"url": CHANNEL_URL})
    assert done.status_code == 200 and done.json() == {"configured": True}
    assert TOKEN not in done.text and TOKEN not in rig.mum.get(PATH).text  # never read back
    assert TOKEN not in rig.stored()  # sealed: not in the file in clear
    assert rig.delete(rig.mum).status_code == 204
    assert rig.mum.get(PATH).json() == {"configured": False}


def test_the_admin_sets_their_own_and_only_theirs(rig: Rig) -> None:
    assert rig.put(rig.env.client, {"url": CHANNEL_URL}).status_code == 200
    assert rig.mum.get(PATH).json() == {"configured": False}  # not shared with the others
    refused = rig.put(rig.env.client, {"url": CHANNEL_URL, "user_id": "x"})
    assert refused.status_code == 422  # no way to name another profile
    assert rig.put(rig.env.client, {"url": "not a url"}).status_code in (400, 422)


def test_a_child_has_no_destination_of_her_own_through_the_api(rig: Rig) -> None:
    for method in ("PUT", "DELETE"):
        got = rig.put(rig.kid, {"url": CHANNEL_URL}) if method == "PUT" else rig.delete(rig.kid)
        assert got.status_code == 403, method
    assert rig.kid.get(PATH).status_code == 403
    assert TOKEN not in rig.stored()
    with rig.engine().connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM notify_destination")).scalar() == 0


def test_an_api_key_sets_nothing(rig: Rig) -> None:
    auth = local_auth(rig.engine(), rig.env.settings)
    for name, username in (("member", "mum"), ("admin", None)):
        key = auth.create_api_key(
            name=name, username=username, read_only=username is not None
        ).value
        bare = rig.env.other_client("10.0.0.77")
        got = bare.put(PATH, json={"url": CHANNEL_URL}, headers={"Authorization": f"Bearer {key}"})
        assert got.status_code == 403
        assert bare.get(PATH, headers={"Authorization": f"Bearer {key}"}).status_code == 403
    assert TOKEN not in rig.stored()


def test_it_is_a_reserved_action_and_needs_a_fresh_session(rig: Rig) -> None:
    action = RESERVED_ACTIONS["notify.profile_destination"]
    assert action.needs_fresh_auth(restricted_profile_exists=False, hardened=False)  # a secret
    rig.env.clock.advance(timedelta(hours=2))
    stale = rig.put(rig.mum, {"url": CHANNEL_URL})
    assert stale.status_code == 401
    assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert TOKEN not in rig.stored()
