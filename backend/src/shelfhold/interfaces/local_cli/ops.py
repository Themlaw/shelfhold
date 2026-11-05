"""The operating commands (EF-INT-023) on the host CLI's two channels.

The socket of the running server serves `ops.*` methods; a stopped server leaves the CLI to call
the same functions itself, under the `flock`. Both take and return plain JSON values, so the CLI
prints one thing whichever channel answered. Every method here runs as the `local_cli` principal,
which is what lets the reserved exits (a secret, the lost keyring...) through: the uid that can
reach `/data` is the admin (R67).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from shelfhold.export.domain.bindings import BindingError
from shelfhold.headless import HeadlessError
from shelfhold.i18n import t
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.local_socket import RpcError, RpcFailure, call
from shelfhold.infrastructure.local_socket.server import APPLICATION_ERROR, INVALID_PARAMS
from shelfhold.ops import OpsError, OpsServices, user_of

__all__ = [
    "OPS_METHODS",
    "LocalChannel",
    "OpsChannel",
    "OpsRefusal",
    "SocketChannel",
    "run_ops_method",
]

Params = Mapping[str, Any]
OpsMethod = Callable[[OpsServices, Principal, Params], object]


class OpsRefusal(Exception):
    """A refusal of the services, with its catalog code (the message holds no secret)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OpsChannel(Protocol):
    def call(self, method: str, **params: object) -> Any: ...


def _text(params: Params, name: str, *, required: bool = True) -> str:
    value = params.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value:
        raise RpcError(INVALID_PARAMS, t("local_cli.param_invalid", name=name, expected="text"))
    return value


def _flag(params: Params, name: str) -> bool:
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise RpcError(INVALID_PARAMS, t("local_cli.param_invalid", name=name, expected="boolean"))
    return value


def _waiting(items: list[Any]) -> list[dict[str, object]]:
    return [dict(vars(item)) for item in items]


def _status(ops: OpsServices, _by: Principal, _p: Params) -> object:
    return ops.status().as_dict()


def _missing_list(ops: OpsServices, _by: Principal, _p: Params) -> object:
    return {"items": _waiting(ops.missing_list())}


def _missing_resolve(ops: OpsServices, by: Principal, p: Params) -> object:
    ops.missing_resolve(
        _text(p, "destination_id"), _text(p, "export_item_id"), _text(p, "action"),
        actor_id=user_of(by),
    )  # fmt: skip
    return {"ok": True}


def _series_resume(ops: OpsServices, _by: Principal, p: Params) -> object:
    ops.series_resume(_text(p, "export_series_id"))
    return {"ok": True}


def _jobs_list(ops: OpsServices, _by: Principal, p: Params) -> object:
    status = _text(p, "status", required=False) or None
    return {
        "items": [
            {"id": j.id, "kind": j.kind, "status": j.status, "error_code": j.error_code,
             "last_error": j.last_error, "attempts": j.attempts}
            for j in ops.jobs_list(status=status)
        ]
    }  # fmt: skip


def _jobs_retry(ops: OpsServices, _by: Principal, p: Params) -> object:
    ops.jobs_retry(_text(p, "job_id"))
    return {"ok": True}


def _target_recheck(ops: OpsServices, _by: Principal, p: Params) -> object:
    outcome = ops.target_recheck(_text(p, "target_id"))
    return {"cleared": list(outcome.cleared), "denied": list(outcome.denied)}


def _target_reconnect(ops: OpsServices, _by: Principal, p: Params) -> object:
    ops.target_reconnect(_text(p, "target_id"))
    return {"ok": True}


def _target_rotate_key(ops: OpsServices, _by: Principal, p: Params) -> object:
    ops.target_rotate_key(_text(p, "target_id"), password=_text(p, "password"))
    return {"ok": True}


def _source_set_secret(ops: OpsServices, _by: Principal, p: Params) -> object:
    ops.source_set_secret(_text(p, "secret"))
    return {"ok": True}


def _source_recheck(ops: OpsServices, _by: Principal, _p: Params) -> object:
    ops.source_recheck()
    return {"ok": True}


def _edition_set_lang(ops: OpsServices, _by: Principal, p: Params) -> object:
    return {"lang": ops.edition_set_lang(_text(p, "edition_id"), _text(p, "lang"))}


def _restore_adopt(ops: OpsServices, by: Principal, p: Params) -> object:
    ops.restore_adopt(confirm_deletion=_flag(p, "confirm_deletion"), principal=by)
    return {"ok": True}


def _accept_lost_keyring(ops: OpsServices, by: Principal, p: Params) -> object:
    waived = ops.tombstone_accept_lost_keyring(confirmation=_text(p, "confirmation"), principal=by)
    return {"waived": waived}


def _set_follow_from(ops: OpsServices, _by: Principal, p: Params) -> object:
    ops.adopt_set_follow_from(
        destination_id=_text(p, "destination_id"), series=_text(p, "series"),
        chapter=_text(p, "chapter"),
    )  # fmt: skip
    return {"ok": True}


def _confirm_migration(ops: OpsServices, _by: Principal, p: Params) -> object:
    ops.adopt_confirm_migration(
        destination_id=_text(p, "destination_id"), fingerprint=_text(p, "fingerprint")
    )
    return {"ok": True}


# method -> (implementation, the reserved action it performs, if any)
OPS_METHODS: Mapping[str, tuple[OpsMethod, str | None]] = {
    "ops.status": (_status, None),
    "ops.missing_list": (_missing_list, None),
    "ops.missing_resolve": (_missing_resolve, None),
    "ops.series_resume": (_series_resume, None),
    "ops.jobs_list": (_jobs_list, None),
    "ops.jobs_retry": (_jobs_retry, None),
    "ops.target_recheck": (_target_recheck, None),
    "ops.target_reconnect": (_target_reconnect, None),
    "ops.target_rotate_key": (_target_rotate_key, "target.rotate_key"),
    "ops.source_set_secret": (_source_set_secret, "source.set_secret"),
    "ops.source_recheck": (_source_recheck, None),
    "ops.edition_set_lang": (_edition_set_lang, None),
    "ops.restore_adopt": (_restore_adopt, "reconcile.confirm_deletion"),
    "ops.tombstone_accept_lost_keyring": (_accept_lost_keyring, "tombstone.accept_lost_keyring"),
    "ops.adopt_set_follow_from": (_set_follow_from, None),
    "ops.adopt_confirm_migration": (_confirm_migration, None),
}


def run_ops_method(ops: OpsServices, principal: Principal, method: str, params: Params) -> object:
    """One call, refusals as `RpcError` (the socket) with the catalog code in `data`."""
    implementation, _reserved = OPS_METHODS[method]
    try:
        return implementation(ops, principal, params)
    except (OpsError, HeadlessError, BindingError) as error:
        raise RpcError(APPLICATION_ERROR, str(error), {"code": error.code}) from None


class LocalChannel:
    """The server is stopped: this process calls the services itself, as the host's admin."""

    def __init__(self, ops: OpsServices) -> None:
        uid = os.getuid()
        self._principal = Principal(
            user_id=str(uid), username=f"uid:{uid}", role="admin", credential=Credential.LOCAL_CLI
        )
        self._ops = ops

    def call(self, method: str, **params: object) -> Any:
        try:
            return run_ops_method(self._ops, self._principal, method, params)
        except RpcError as error:
            code = (error.data or {}).get("code", "")
            raise OpsRefusal(str(code), str(error)) from None


class SocketChannel:
    def __init__(self, path: Path) -> None:
        self._path = path

    def call(self, method: str, **params: object) -> Any:
        try:
            return call(self._path, method, {k: v for k, v in params.items() if v is not None})
        except RpcFailure as failure:
            code = (failure.data or {}).get("code")
            if isinstance(code, str):
                raise OpsRefusal(code, failure.message) from None
            raise
