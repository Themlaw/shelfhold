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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from shelfhold.export.domain.bindings import BindingError
from shelfhold.headless import HeadlessError
from shelfhold.i18n import t
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.local_socket import Peer, RpcError, RpcFailure, call
from shelfhold.infrastructure.local_socket.server import APPLICATION_ERROR, INVALID_PARAMS
from shelfhold.interfaces.local_cli.principal import principal_for_peer
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
        destination_id=_dest(p), series=_text(p, "series"),
        chapter=_text(p, "chapter"),
    )  # fmt: skip
    return {"ok": True}


def _confirm_migration(ops: OpsServices, _by: Principal, p: Params) -> object:
    ops.adopt_confirm_migration(destination_id=_dest(p), fingerprint=_text(p, "fingerprint"))
    return {"ok": True}


def _optional(params: Params, name: str) -> str | None:
    value = params.get(name)
    return _text(params, name) if value is not None else None


def _number(params: Params, name: str, default: float) -> float:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RpcError(INVALID_PARAMS, t("local_cli.param_invalid", name=name, expected="number"))
    return float(value)


def _dest(params: Params) -> str:
    return _optional(params, "destination_id") or ""


def _pair_propose(ops: OpsServices, _by: Principal, p: Params) -> object:
    return {"proposals": ops.library.pair_propose(_dest(p))}


def _pair_confirm(ops: OpsServices, _by: Principal, p: Params) -> object:
    backfill = p.get("confirm_backfill")
    return ops.library.pair_confirm(
        _dest(p), _text(p, "export_series_id"),
        source_key=_text(p, "source_key"), remote_id=_text(p, "remote_id"),
        follow_from=_optional(p, "follow_from"),
        confirm_backfill=backfill if isinstance(backfill, int) else None,
    )  # fmt: skip


def _pair_auto(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.library.pair_auto(
        _dest(p), min_score=_number(p, "min_score", 0.9),
        fingerprint=_optional(p, "fingerprint"),
    )  # fmt: skip


def _migration_report(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.library.migration_report(_dest(p))


def _repatriate_plan(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.library.repatriate_plan(_dest(p), _optional(p, "source"))


def _repatriate_apply(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.library.repatriate_apply(
        _dest(p), fingerprint=_text(p, "fingerprint"),
        source=_optional(p, "source"),
    )  # fmt: skip


def _repatriate_check(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.library.repatriate_check(_dest(p))


def _give_back(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.library.give_back_upstream(
        shelf_id=_optional(p, "shelf_id"), edition_id=_optional(p, "edition_id")
    )


def _purge(ops: OpsServices, by: Principal, p: Params) -> object:
    kind = _text(p, "kind")
    if kind not in {"unit", "work"}:
        raise RpcError(
            INVALID_PARAMS, t("local_cli.param_invalid", name="kind", expected="unit|work")
        )
    return ops.library.purge(
        kind=kind, anchor_id=_text(p, "anchor_id"), typed=_optional(p, "typed"),
        principal=by, now=datetime.now(UTC),
    )  # fmt: skip


def _probe(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.library.probe(_text(p, "folder"))


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
    "ops.pair_propose": (_pair_propose, None),
    "ops.pair_confirm": (_pair_confirm, None),
    "ops.pair_auto": (_pair_auto, None),
    "ops.migration_report": (_migration_report, None),
    "ops.repatriate_plan": (_repatriate_plan, None),
    "ops.repatriate_apply": (_repatriate_apply, None),
    "ops.repatriate_check": (_repatriate_check, None),
    "ops.detach_restore_upstream": (_give_back, None),
    "ops.purge": (_purge, "purge.now"),
    "ops.probe": (_probe, None),
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
        self._principal = principal_for_peer(Peer(os.getpid(), os.getuid(), os.getgid()))
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
