"""The methods the local socket serves: the identity commands of the host CLI (EF-USR-024).

Each method calls the application facade (`LocalAuth`) with the peer's `local_cli` principal, so
the audit says `local_cli:<uid>`. A method that is a reserved action goes through
`authorize_reserved` first, so its rule stays in one place. Nothing here is HTTP.
"""

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from shelfhold.i18n import t
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    AdminExistsError,
    Allowed,
    CreatedApiKey,
    LocalAuth,
    Principal,
    UnknownUserError,
    authorize_reserved,
)
from shelfhold.infrastructure.local_socket import Peer, RpcError
from shelfhold.infrastructure.local_socket.server import (
    APPLICATION_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
)
from shelfhold.interfaces.local_cli.principal import principal_for_peer

Params = Mapping[str, Any]
Method = Callable[[LocalAuth, Principal, Params], object]
_PASSWORD_REASONS = frozenset(
    {"invalid_username", "password_too_short", "password_too_long", "invalid_expiry"}
)


def _invalid(name: str, expected: str) -> RpcError:
    return RpcError(INVALID_PARAMS, t("local_cli.param_invalid", name=name, expected=expected))


def _text(params: Params, name: str, *, required: bool = True) -> str | None:
    value = params.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise _invalid(name, "text")
    return value


def _flag(params: Params, name: str) -> bool:
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise _invalid(name, "boolean")
    return value


def _days(params: Params) -> int | None:
    value = params.get("expires_days")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid("expires_days", "integer")
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _key(created: CreatedApiKey) -> dict[str, object]:
    return {
        "value": created.value,
        "prefix": created.prefix,
        "expires_at": _iso(created.expires_at),
    }


def _create_admin(auth: LocalAuth, by: Principal, params: Params) -> object:
    return auth.create_admin(str(_text(params, "username")), str(_text(params, "password")), by=by)


def _reset_password(auth: LocalAuth, by: Principal, params: Params) -> object:
    return auth.reset_password(
        str(_text(params, "username")), str(_text(params, "password")), by=by
    )


def _sessions_revoke(auth: LocalAuth, by: Principal, params: Params) -> object:
    return auth.revoke_sessions(
        username=_text(params, "username", required=False),
        session_id=_text(params, "session_id", required=False),
        by=by,
    )


def _keys_create(auth: LocalAuth, by: Principal, params: Params) -> object:
    return _key(
        auth.create_api_key(
            name=_text(params, "name", required=False),
            read_only=_flag(params, "read_only"),
            expires_days=_days(params),
            by=by,
        )
    )


def _keys_rotate(auth: LocalAuth, by: Principal, params: Params) -> object:
    rotated = auth.rotate_api_key(str(_text(params, "prefix")), expires_days=_days(params), by=by)
    return None if rotated is None else _key(rotated)


def _keys_revoke(auth: LocalAuth, by: Principal, params: Params) -> object:
    return auth.revoke_api_key(str(_text(params, "prefix")), by=by)


def _keys_list(auth: LocalAuth, _by: Principal, _params: Params) -> object:
    return [
        {
            "prefix": key.prefix,
            "name": key.name,
            "scopes": list(key.scopes),
            "read_only": key.read_only,
            "created_at": _iso(key.created_at),
            "expires_at": _iso(key.expires_at),
            "last_used_at": _iso(key.last_used_at),
            "revoked": key.revoked_at is not None,
            "revoked_at": _iso(key.revoked_at),
        }
        for key in auth.api_keys()
    ]


# method -> (implementation, the reserved action it is, if any). A key is a credential: managing
# credentials is reserved (EF-USR-024), and `local_cli` is one of the two channels allowed to.
METHODS: Mapping[str, tuple[Method, str | None]] = {
    "ping": (lambda _a, _b, _c: "pong", None),
    "identity.create_admin": (_create_admin, None),
    "identity.reset_password": (_reset_password, None),
    "identity.sessions_revoke": (_sessions_revoke, None),
    "identity.keys_create": (_keys_create, "keys.create"),
    "identity.keys_rotate": (_keys_rotate, "keys.rotate"),
    "identity.keys_revoke": (_keys_revoke, "keys.revoke"),
    "identity.keys_list": (_keys_list, None),
}


class IdentityDispatcher:
    """The `Handler` of the socket server."""

    def __init__(self, auth: LocalAuth) -> None:
        self._auth = auth

    async def __call__(self, peer: Peer, method: str, params: dict[str, Any]) -> object:
        entry = METHODS.get(method)
        if entry is None:
            raise RpcError(METHOD_NOT_FOUND, t("local_cli.unknown_method", method=method))
        implementation, reserved = entry
        principal = principal_for_peer(peer)
        if reserved is not None:
            verdict = authorize_reserved(
                principal,
                RESERVED_ACTIONS[reserved],
                now=self._auth.now(),
                fresh_auth_s=self._auth.fresh_auth_s,
                hardened=self._auth.hardened,
            )
            if not isinstance(verdict, Allowed):
                raise RpcError(
                    APPLICATION_ERROR, t("local_cli.not_allowed"), {"code": type(verdict).__name__}
                )
        try:
            return await asyncio.to_thread(implementation, self._auth, principal, params)
        except UnknownUserError:
            raise RpcError(
                APPLICATION_ERROR,
                t("admin.unknown_user", username=params.get("username", "")),
                {"code": "unknown_user"},
            ) from None
        except AdminExistsError:
            raise RpcError(APPLICATION_ERROR, t("admin.exists"), {"code": "admin_exists"}) from None
        except ValueError as error:
            if str(error) in _PASSWORD_REASONS:
                raise RpcError(APPLICATION_ERROR, str(error), {"code": str(error)}) from None
            raise
        except RuntimeError as error:
            if str(error) == "api_key_prefix_exhausted":
                raise RpcError(
                    APPLICATION_ERROR,
                    t("admin.key_rotation_failed"),
                    {"code": "key_rotation_failed"},
                ) from None
            raise
