"""SEC-1 (EF-USR-024): one rule says who holds the administrator's power — her session, or her key
of scope `admin` that may write. A read-only key of the admin (whatever scopes it carries) is a
program reading as her profile: every guard of an admin surface refuses it."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.requests import Request

from shelfhold.identity.public import Credential, Principal
from shelfhold.interfaces.http_api import activity, auth, health, plans, stats
from shelfhold.interfaces.http_api.ops_dashboard import require_admin_surface
from shelfhold.interfaces.http_api.problems import ProblemError

pytestmark = pytest.mark.req("EF-USR-024")


def _principal(role: str, credential: Credential, scopes: tuple[str, ...], read_only: bool) -> Any:
    return Principal(
        user_id="u1", username="u", role=role, credential=credential, scopes=scopes,
        read_only=read_only, key_id=None if credential is Credential.SESSION else "k1",
        session_id="s1" if credential is Credential.SESSION else None,
    )  # fmt: skip


ADMIN_SESSION = _principal("admin", Credential.SESSION, (), False)
ADMIN_KEY = _principal("admin", Credential.API_KEY, ("admin",), False)
ADMIN_READ_KEY = _principal("admin", Credential.API_KEY, ("read",), True)
ADMIN_METRICS_KEY = _principal("admin", Credential.API_KEY, ("metrics",), True)
# never made since SEC-1 (A3), and refused all the same if one was in the database
ADMIN_SCOPE_READ_ONLY = _principal("admin", Credential.API_KEY, ("admin",), True)
MEMBER_SESSION = _principal("member", Credential.SESSION, (), False)
DEVICE = _principal("admin", Credential.DEVICE_TOKEN, (), True)

ALLOWED = (ADMIN_SESSION, ADMIN_KEY)
REFUSED = (ADMIN_READ_KEY, ADMIN_METRICS_KEY, ADMIN_SCOPE_READ_ONLY, MEMBER_SESSION, DEVICE)


def _request(principal: Principal, method: str = "GET") -> Request:
    return Request(
        {"type": "http", "method": method, "path": "/", "headers": [],
         "state": {"principal": principal}}
    )  # fmt: skip


@pytest.mark.parametrize("principal", ALLOWED)
def test_the_admin_session_and_her_writing_admin_key_hold_the_power(principal: Principal) -> None:
    assert auth.holds_admin_power(principal)
    assert require_admin_surface(principal) is principal
    assert stats.require_admin(principal) is principal
    assert plans.require_admin(_request(principal)) is principal
    assert health._is_admin(_request(principal))


@pytest.mark.parametrize("principal", REFUSED)
def test_a_read_key_of_the_admin_holds_none_of_it(principal: Principal) -> None:
    assert not auth.holds_admin_power(principal)
    with pytest.raises(ProblemError):
        require_admin_surface(principal)
    with pytest.raises(ProblemError):
        stats.require_admin(principal)
    with pytest.raises(ProblemError):
        plans.require_admin(_request(principal))
    assert not health._is_admin(_request(principal))


def test_the_shared_guard_is_the_one_of_every_admin_module() -> None:
    """`stats.require_admin` is imported by some twenty modules: it is the shared rule itself."""
    assert stats.require_admin is require_admin_surface


def test_the_activity_of_a_read_key_of_the_admin_is_never_the_admins() -> None:
    """A key reads the activity as the integration role: the admin's payload never leaves."""
    viewer = activity.viewer_of(ADMIN_READ_KEY)
    assert viewer.role != "admin"
    assert activity.family_payload(viewer.role, "download_request", {"state": "x"}) == {}
