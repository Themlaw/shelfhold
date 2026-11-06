"""The methods the local socket serves: the identity commands of the host CLI (EF-USR-024).

Each method calls the application facade (`LocalAuth`) with the peer's `local_cli` principal, so
the audit says `local_cli:<uid>`. A method that is a reserved action goes through
`authorize_reserved` first, so its rule stays in one place. Nothing here is HTTP.
"""

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from shelfhold.export.domain.bindings import BindingError
from shelfhold.headless import HeadlessAdmin, HeadlessError, Plan
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
from shelfhold.interfaces.local_cli.ops import OPS_METHODS, run_ops_method
from shelfhold.interfaces.local_cli.principal import principal_for_peer
from shelfhold.ops import OpsServices

Params = Mapping[str, Any]
Method = Callable[[LocalAuth, Principal, Params], object]
HeadlessMethod = Callable[[HeadlessAdmin, Params], object]
ACTOR = "_actor"  # the audit actor of the peer, written by the dispatcher over any given value
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


# -- headless configuration (EF-INT-024): the same facade as the HTTP routes --------------------


def _plain(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


def _config_value(row: Any) -> dict[str, object]:
    return {"key": row.key, "value": row.value, "source": row.source, "default": row.default,
            "overridden_by": row.overridden_by, "warning": row.warning}  # fmt: skip


def _plan(plan: Plan) -> dict[str, object]:
    return {
        "noop": plan.is_noop,
        "rendered": plan.render(),
        "changes": [
            {"kind": c.kind, "subject": c.subject, "action": c.action, "before": c.before,
             "after": c.after, "secret": c.secret}
            for c in plan.changes
        ],
    }  # fmt: skip


def _mapping(params: Params, name: str) -> dict[str, str]:
    value = params.get(name) or {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise _invalid(name, "table")
    return value


def _config_get(admin: HeadlessAdmin, params: Params) -> object:
    return _config_value(admin.get(str(_text(params, "key"))))


def _config_list(admin: HeadlessAdmin, _params: Params) -> object:
    return [_config_value(row) for row in admin.list()]


def _config_set(admin: HeadlessAdmin, params: Params) -> object:
    return _config_value(admin.set(str(_text(params, "key")), str(_text(params, "value"))))


def _config_apply(admin: HeadlessAdmin, params: Params) -> object:
    return _plan(
        admin.apply_text(
            str(_text(params, "text")),
            base_dir=Path(str(_text(params, "base_dir"))),
            dry_run=_flag(params, "dry_run"),
            secrets=_mapping(params, "secrets"),
        )
    )


def _target_add(admin: HeadlessAdmin, params: Params) -> object:
    if params.get("kind", "kavita") != "kavita":
        raise _invalid("kind", "kavita")
    path_map = params.get("path_map")
    added = admin.target_add_kavita(
        name=str(_text(params, "name")),
        url=str(_text(params, "url")),
        username=str(_text(params, "username")),
        password=str(_text(params, "password")),
        accept_admin_risk=_flag(params, "accept_admin_risk"),
        path_map=path_map if isinstance(path_map, list) else None,
        actor=_text(params, ACTOR, required=False),  # set by the dispatcher, never by the peer
    )
    return {"id": added.id, "name": added.name, "created": added.created,
            "key_expires_at": _plain(added.key_expires_at)}  # fmt: skip


def _source_add(admin: HeadlessAdmin, params: Params) -> object:
    if params.get("kind", "suwayomi") != "suwayomi":
        raise _invalid("kind", "suwayomi")
    added = admin.source_add_suwayomi(
        url=str(_text(params, "url")), secret=_text(params, "secret", required=False)
    )
    return {"id": added.plugin_id, "auth_mode": added.auth_mode, "created": added.created}


def _subpath(params: Params) -> str:
    value = params.get("subpath", "")
    if not isinstance(value, str):
        raise _invalid("subpath", "text")
    return value  # empty is the root of the destination


def _shelf_create(admin: HeadlessAdmin, params: Params) -> object:
    created = admin.shelf_create(
        name=str(_text(params, "name")),
        max_age=str(_text(params, "max_age")),
        target=str(_text(params, "target")),
        subpath=_subpath(params),
        phrase=_text(params, "phrase", required=False),
    )
    return {"shelf_id": created.shelf_id, "destination_id": created.destination_id,
            "created": created.created}  # fmt: skip


# method -> (implementation, the reserved action it performs, if any). `local_cli` may do every
# reserved action; a key may not: it has no way to this socket.
HEADLESS_METHODS: Mapping[str, tuple[HeadlessMethod, str | None]] = {
    "config.get": (_config_get, None),
    "config.list": (_config_list, None),
    "config.set": (_config_set, None),
    "config.apply": (_config_apply, None),
    "target.add": (_target_add, "admin_risk.accept"),
    "source.add": (_source_add, "source.set_secret"),
    "shelf.create": (_shelf_create, "shelf.max_age_rating"),
}


class IdentityDispatcher:
    """The `Handler` of the socket server."""

    def __init__(
        self,
        auth: LocalAuth,
        headless: HeadlessAdmin | None = None,
        ops: OpsServices | None = None,
    ) -> None:
        self._auth = auth
        self._headless = headless
        self._ops = ops

    def _authorize(self, principal: Principal, reserved: str | None) -> None:
        if reserved is None:
            return
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

    async def __call__(self, peer: Peer, method: str, params: dict[str, Any]) -> object:
        operating = OPS_METHODS.get(method)
        if operating is not None and self._ops is not None:  # EF-INT-023
            principal = principal_for_peer(peer)
            self._authorize(principal, operating[1])
            return await asyncio.to_thread(run_ops_method, self._ops, principal, method, params)
        headless = HEADLESS_METHODS.get(method)
        if headless is not None and self._headless is not None:
            principal = principal_for_peer(peer)
            self._authorize(principal, headless[1])
            audited = {**params, ACTOR: principal.actor_id}  # whatever the peer sent is replaced
            return await self._run_headless(headless[0], self._headless, audited)
        entry = METHODS.get(method)
        if entry is None:
            raise RpcError(METHOD_NOT_FOUND, t("local_cli.unknown_method", method=method))
        implementation, reserved = entry
        principal = principal_for_peer(peer)
        self._authorize(principal, reserved)
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

    @staticmethod
    async def _run_headless(
        implementation: HeadlessMethod, admin: HeadlessAdmin, params: Params
    ) -> object:
        try:
            return await asyncio.to_thread(implementation, admin, params)
        except (HeadlessError, BindingError) as error:
            raise RpcError(APPLICATION_ERROR, str(error), {"code": error.code}) from None
