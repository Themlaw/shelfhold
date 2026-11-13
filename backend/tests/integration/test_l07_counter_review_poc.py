"""Counter-review PoC of PR #210 (M3 L07): the M2 fix leaves the rehash path open.

Red on the branch under review; see `docs/decisions/M3.md`, section "L07 : contre-relecture".
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.auth_env import PASSWORD, AuthEnv
from tests.integration.test_l07_sessions import (  # noqa: F401 - `api` is the fixture
    NEW_PASSWORD,
    _user,
    api,
)


@pytest.mark.req("EF-USR-011")
def test_counter_poc_a_rehash_cannot_undo_a_reset_that_raced_the_sign_in(
    api: AuthEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`login` verifies the hash it read, then, when the hasher asks for it (new argon2 cost after
    an upgrade), writes the new hash of the OLD password with no condition. A reset that commits
    in between is overwritten: the session opens (the re-read under the lock compares with the
    hash `login` just wrote) and the host's new password is gone."""
    _user(api, "mum", "member", "10.0.0.3")
    auth: Any = api.app.state.auth_gate.auth  # type: ignore[attr-defined]
    real_verify = auth._hasher.verify
    raced: list[int] = []

    def verify_then_reset(stored: Any, password: str) -> bool:
        ok = bool(real_verify(stored, password))
        if not raced:
            raced.append(auth.reset_password("mum", NEW_PASSWORD))
        return ok

    monkeypatch.setattr(auth._hasher, "needs_rehash", lambda _stored: True)
    monkeypatch.setattr(auth._hasher, "verify", verify_then_reset)
    thief = api.other_client("10.0.0.66")
    signed = thief.post("/api/v1/auth/login", json={"username": "mum", "password": PASSWORD})
    monkeypatch.setattr(auth._hasher, "verify", real_verify)
    assert raced, "the reset did not run"
    assert signed.status_code != 200 or thief.get("/api/v1/me").status_code == 401
    host = api.other_client("10.0.0.4")
    assert (
        host.post(
            "/api/v1/auth/login", json={"username": "mum", "password": NEW_PASSWORD}
        ).status_code
        == 200
    ), "the reset's password was overwritten by the rehash of the old one"
