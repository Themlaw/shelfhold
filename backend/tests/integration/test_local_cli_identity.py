"""Identity commands over the local socket (EF-USR-024, ENF-SEC-010, ENF-OPS-015).

The principal is `local_cli`, the actor of the audit is `local_cli:<uid>`, and a revoked session
stops working with the server still running (PLAN-M1a section 7, L05/L23).
"""

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends

from shelfhold.config import Settings
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    Credential,
    CredentialNotAllowed,
    Effect,
    Principal,
    ReservedAction,
    authorize_reserved,
)
from shelfhold.infrastructure.local_socket import Peer, RpcFailure, call
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.local_cli import principal_for_peer
from tests.auth_env import ADMIN, PASSWORD, AuthEnv, auth_env, db_rows

KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")
PURGE = ReservedAction("test.purge_now", frozenset({Effect.DELETES_PERMANENTLY}))
ACTOR = f"local_cli:{os.getuid()}"


def _router(effects: list[str]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/test")

    @router.post("/purge")
    def purge(principal: Principal = Depends(reserved_action(PURGE))) -> dict[str, str]:  # noqa: B008
        effects.append(principal.actor_id)
        return {"ok": "purged"}

    return router


@pytest.fixture
def effects() -> list[str]:
    return []


@pytest.fixture
def api(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, effects: list[str]
) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, routers=[_router(effects)]) as env:
        yield env


def _socket(api: AuthEnv) -> Path:
    return api.settings.data_dir / "shelfhold.sock"


def _audit(api: AuthEnv) -> list[tuple[object, ...]]:
    return db_rows(api.settings, "SELECT actor_id, action, target FROM audit ORDER BY at, id")


@pytest.mark.req("EF-USR-024", "ENF-OPS-015")
def test_the_running_server_listens_on_the_data_socket(api: AuthEnv) -> None:
    assert call(_socket(api), "identity.keys_list", {}) == []


@pytest.mark.req("EF-USR-024")
def test_keys_create_via_socket_audited_as_local_cli_uid(api: AuthEnv) -> None:
    created = call(_socket(api), "identity.keys_create", {"name": "ci", "read_only": False})
    assert isinstance(created, dict)
    assert KEY.fullmatch(created["value"])
    rows = [row for row in _audit(api) if row[1] == "api_key.created"]
    assert rows == [(ACTOR, "api_key.created", created["prefix"])]  # no host uid suffix: a principal
    assert "uid=" not in str(rows)


@pytest.mark.req("EF-USR-024")
def test_keys_rotate_and_revoke_via_socket_audited_as_local_cli(api: AuthEnv) -> None:
    first = call(_socket(api), "identity.keys_create", {"name": "ci"})
    rotated = call(_socket(api), "identity.keys_rotate", {"prefix": first["prefix"]})  # type: ignore[index]
    assert call(_socket(api), "identity.keys_revoke", {"prefix": rotated["prefix"]}) is True  # type: ignore[index]
    audit = {action: (actor, target) for actor, action, target in _audit(api)}  # type: ignore[misc]
    assert audit["api_key.rotated"] == (ACTOR, f"{first['prefix']}>{rotated['prefix']}")  # type: ignore[index]
    assert audit["api_key.revoked"] == (ACTOR, rotated["prefix"])  # type: ignore[index]
    keys = call(_socket(api), "identity.keys_list", {})
    assert {k["prefix"]: k["revoked"] for k in keys} == {  # type: ignore[union-attr]
        first["prefix"]: True,  # type: ignore[index]
        rotated["prefix"]: True,  # type: ignore[index]
    }


@pytest.mark.req("EF-USR-024")
def test_revoking_a_stolen_session_via_socket_needs_no_server_stop(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    assert api.client.get("/api/v1/auth/session").status_code == 200
    closed = call(_socket(api), "identity.sessions_revoke", {"username": ADMIN})
    assert closed == 1
    assert api.client.get("/api/v1/auth/session").status_code == 401
    assert (ACTOR, "auth.sessions_revoked", _audit(api)[-1][2]) == _audit(api)[-1]


@pytest.mark.req("EF-USR-024")
def test_password_reset_via_socket_closes_sessions(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    closed = call(
        _socket(api),
        "identity.reset_password",
        {"username": ADMIN, "password": "an entirely new passphrase"},
    )
    assert closed == 1
    assert api.login(password=PASSWORD).status_code == 401
    assert api.login(password="an entirely new passphrase").status_code == 200  # noqa: S106


@pytest.mark.req("EF-USR-024")
def test_domain_errors_cross_the_socket_as_codes_not_tracebacks(api: AuthEnv) -> None:
    with pytest.raises(RpcFailure) as unknown:
        call(_socket(api), "identity.sessions_revoke", {"username": "nobody"})
    assert unknown.value.data == {"code": "unknown_user"}
    with pytest.raises(RpcFailure) as short:
        call(
            _socket(api), "identity.reset_password", {"username": ADMIN, "password": "short"}
        )
    assert short.value.data == {"code": "password_too_short"}
    with pytest.raises(RpcFailure) as bad:
        call(_socket(api), "identity.keys_create", {"expires_days": "soon"})
    assert bad.value.code == -32602
    with pytest.raises(RpcFailure) as missing:
        call(_socket(api), "identity.keys_revoke", {})
    assert missing.value.code == -32602
    with pytest.raises(RpcFailure) as nothing:
        call(_socket(api), "identity.format_disk", {})
    assert nothing.value.code == -32601


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_reserved_action_allowed_via_local_cli(api: AuthEnv) -> None:
    peer = Peer(pid=os.getpid(), uid=os.getuid(), gid=os.getgid())
    principal = principal_for_peer(peer)
    assert principal.credential is Credential.LOCAL_CLI
    assert principal.actor_id == ACTOR
    now = datetime.now(UTC)
    for action in RESERVED_ACTIONS.values():
        assert isinstance(authorize_reserved(principal, action, now=now), Allowed), action.name
    # ... and the same actions stay closed to a key, whatever its scope
    key = Principal("u", "u", "admin", Credential.API_KEY, key_id="k", scopes=("admin",))
    for action in RESERVED_ACTIONS.values():
        assert isinstance(authorize_reserved(key, action, now=now), CredentialNotAllowed)
    # key management, a reserved action, works through the socket
    assert call(_socket(api), "identity.keys_create", {})


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_loopback_http_never_counts_as_local_cli(
    api: AuthEnv, effects: list[str]
) -> None:
    created = call(_socket(api), "identity.keys_create", {})
    headers = {
        "Authorization": f"Bearer {created['value']}",  # type: ignore[index]
        "X-Forwarded-For": "127.0.0.1",
        "X-Real-IP": "127.0.0.1",
        "X-Shelfhold-Peer-Uid": str(os.getuid()),
        "Remote-User": ADMIN,
    }
    for client in (api.client, api.other_client("127.0.0.1"), api.other_client("::1")):
        anonymous = client.post("/api/v1/test/purge", headers={"X-Forwarded-For": "127.0.0.1"})
        assert anonymous.status_code == 401
        keyed = client.post("/api/v1/test/purge", headers=headers)
        assert keyed.status_code == 403
        assert keyed.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert effects == []
    assert not any(row[0] == ACTOR and row[1] == "test.purge_now" for row in _audit(api))
