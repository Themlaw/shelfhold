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
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.local_socket import Peer, RpcError, RpcFailure, call
from shelfhold.infrastructure.local_socket.server import APPLICATION_ERROR, INVALID_PARAMS
from shelfhold.interfaces.local_cli.principal import principal_for_peer
from shelfhold.ops import CodedRefusal, OpsError, OpsServices, user_of

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


def _jobs_run(ops: OpsServices, _by: Principal, p: Params) -> object:
    ops.jobs_run_now(_text(p, "kind"))
    return {"ok": True}


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


def _source_reconnect(ops: OpsServices, _by: Principal, _p: Params) -> object:
    ops.source_reconnect()
    return {"ok": True}


def _edition_set_lang(ops: OpsServices, _by: Principal, p: Params) -> object:
    return {"lang": ops.edition_set_lang(_text(p, "edition_id"), _text(p, "lang"))}


def _edition_set_series_name(ops: OpsServices, _by: Principal, p: Params) -> object:
    series_id, name = ops.edition_set_series_name(_text(p, "edition_id"), _text(p, "name"))
    return {"export_series_id": series_id, "name": name}


def _restore_adopt(ops: OpsServices, by: Principal, p: Params) -> object:
    ops.restore_adopt(confirm_deletion=_flag(p, "confirm_deletion"), principal=by)
    return {"ok": True}


def _accept_lost_keyring(ops: OpsServices, by: Principal, p: Params) -> object:
    waived = ops.tombstone_accept_lost_keyring(confirmation=_text(p, "confirmation"), principal=by)
    return {"waived": waived}


def _secrets_import(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.secrets_import(
        passphrase_file=_text(p, "passphrase_file"), in_file=_text(p, "in_file"), principal=by
    )


def _set_aside_journal(ops: OpsServices, by: Principal, p: Params) -> object:
    root = _text(p, "root_id", required=False) or None
    return {"aside": ops.tombstone_set_aside_journal(principal=by, root_id=root)}


def _tombstone_rekey(ops: OpsServices, by: Principal, p: Params) -> object:
    kids = [k for k in _text(p, "kids").split(",") if k]
    return ops.tombstone_rekey(principal=by, kids=kids)


def _tombstone_flush(ops: OpsServices, _by: Principal, _p: Params) -> object:
    return {"still_owed": ops.tombstone_flush()}


def _set_follow_from(ops: OpsServices, _by: Principal, p: Params) -> object:
    ops.adopt_set_follow_from(
        destination_id=_dest(p), series=_text(p, "series"),
        chapter=_text(p, "chapter"),
    )  # fmt: skip
    return {"ok": True}


def _set_coverage(ops: OpsServices, _by: Principal, p: Params) -> object:
    added = ops.adopt_set_coverage(
        destination_id=_dest(p), series=_text(p, "series"), volume=_text(p, "volume"),
        first=_text(p, "first"), last=_text(p, "last"),
    )  # fmt: skip
    return {"ok": True, "added": added}


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


def _pair_decline(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.library.pair_decline(_text(p, "export_series_id"))


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
        principal=by,
    )  # fmt: skip


def _kept_ack(ops: OpsServices, by: Principal, _p: Params) -> object:
    return ops.library.kept_ack(principal=by)


def _settle_log(ops: OpsServices, _by: Principal, _p: Params) -> object:
    return ops.library.settle_log()


def _leftovers_plan(ops: OpsServices, _by: Principal, _p: Params) -> object:
    return ops.library.leftovers_plan()


def _leftovers_apply(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.library.leftovers_apply(if_match=_text(p, "if_match"), principal=by)


def _intents_list(ops: OpsServices, _by: Principal, _p: Params) -> object:
    return {"items": ops.intents.list_open()}


def _intents_resolve(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.intents.resolve(
        _text(p, "intent_id"), action=_text(p, "action"), confirm=_optional(p, "confirm"),
        principal=by,
    )  # fmt: skip


def _revisions_list(ops: OpsServices, _by: Principal, _p: Params) -> object:
    return ops.revisions.list_pending()


def _revisions_apply(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.revisions.apply(
        confirm=_optional(p, "confirm"), include_read=bool(p.get("include_read")),
        principal=by,
    )  # fmt: skip


def _revisions_force(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.revisions.apply(
        confirm=_optional(p, "confirm"), include_read=bool(p.get("include_read")),
        force_ids=tuple(str(i) for i in p.get("force_ids") or ()), principal=by,
    )  # fmt: skip


def _normalize_migrate(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.normalize.migrate(_text(p, "name"), confirm=_optional(p, "confirm"), principal=by)


def _content_reneutralize(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.content.reneutralize(confirm=_optional(p, "confirm"))


def _meta_rebuild(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.meta.rebuild(unit=_optional(p, "unit"))


def _meta_check(ops: OpsServices, _by: Principal, _p: Params) -> object:
    return ops.meta.check()


def _meta_enrich(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.meta.enrich(_text(p, "target"))


def _count(params: Params, name: str) -> int | None:
    value = params.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RpcError(INVALID_PARAMS, t("local_cli.param_invalid", name=name, expected="integer"))
    return value


def _backfill_estimate(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.backfill.estimate(_text(p, "edition_id"), _text(p, "mode"))


def _backfill_start(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.backfill.start(
        _text(p, "edition_id"), _text(p, "mode"),
        confirm_units=_count(p, "confirm_units"), confirm_bytes=_count(p, "confirm_bytes"),
    )  # fmt: skip


def _follow(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.follows.follow(
        _text(p, "source_key"), _text(p, "remote_id"), requester_user_id=user_of(by)
    )


def _unfollow(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.follows.unfollow(
        _text(p, "source_key"), _text(p, "remote_id"),
        remove_upstream=_flag(p, "remove_upstream"), confirm=_flag(p, "confirm"),
    )  # fmt: skip


def _source_search(ops: OpsServices, _by: Principal, p: Params) -> object:
    filters = p.get("filters")
    return ops.acquire.search(
        _text(p, "source_key"), _text(p, "query", required=False),
        filters=dict(filters) if isinstance(filters, Mapping) else None,
        cursor=_optional(p, "cursor"),
    )  # fmt: skip


def _source_filters(ops: OpsServices, _by: Principal, p: Params) -> object:
    return {"filters": ops.acquire.filters(_text(p, "source_key"))}


def _acquire(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.acquire.acquire(
        url=_optional(p, "url"), source_key=_optional(p, "source_key"),
        remote_id=_optional(p, "remote_id"), unit=_optional(p, "unit"),
        requester_user_id=user_of(by),
    )  # fmt: skip


def _relink(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.library.relink(
        _text(p, "work_id"), source_key=_text(p, "source_key"), remote_id=_text(p, "remote_id"),
        lang=_optional(p, "lang"), replaces=_optional(p, "replaces"), principal=by,
    )  # fmt: skip


def _withdraw_plan(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.library.withdraw_plan(_optional(p, "shelf_id"))


def _withdraw_apply(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.library.withdraw_apply(
        if_match=_text(p, "if_match"), shelf_id=_optional(p, "shelf_id"), principal=by
    )


def _shelf_purge_upstream(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.library.shelf_purge_upstream(
        shelf=_text(p, "shelf"), enabled=bool(p.get("enabled")), principal=by
    )


def _rate(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.rating.rate(
        _text(p, "age"),
        work_id=_optional(p, "work_id"),
        edition_id=_optional(p, "edition_id"),
        principal=by,
    )


def _attach_list(ops: OpsServices, _by: Principal, _p: Params) -> object:
    return ops.attach.attachments()


def _attach_detach(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.attach.separate(_text(p, "attach_id"), principal=by)


def _attach_plan(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.attach.plan(_text(p, "attach_id"))


def _attach_confirm(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.attach.confirm(_text(p, "attach_id"), confirm=_text(p, "confirm"), principal=by)


def _attach_decline(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.attach.decline(_text(p, "attach_id"), principal=by)


def _edition_merge(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.edition_merge.merge(
        _text(p, "keep"), _text(p, "drop"), confirm=_optional(p, "confirm"), principal=by
    )


def _edition_merge_proposals(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.edition_merge.proposals(_text(p, "keep"), _text(p, "drop"), principal=by)


def _rate_pending(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.rating.pending(_text(p, "age"))


def _rate_batch(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.rating.rate_batch(_text(p, "age"), confirm=_text(p, "confirm"), principal=by)


def _root_takeover(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.root_takeover(_text(p, "root"), principal=by)


def _root_reinit(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.root_reinit(_text(p, "root"), principal=by)


def _root_restored(ops: OpsServices, by: Principal, p: Params) -> object:
    return ops.root_restored(_text(p, "root"), principal=by)


def _store_reinit(ops: OpsServices, by: Principal, _p: Params) -> object:
    return ops.store_reinit(principal=by)


def _restore_acknowledge(ops: OpsServices, by: Principal, _p: Params) -> object:
    return {"ok": True, "passkey_changes": ops.restore_acknowledge(principal=by)}


def _rebaseline_inodes(ops: OpsServices, _by: Principal, p: Params) -> object:
    return ops.reconcile_rebaseline_inodes(_optional(p, "destination_id"))


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
    "ops.jobs_run": (_jobs_run, None),
    "ops.target_recheck": (_target_recheck, None),
    "ops.target_reconnect": (_target_reconnect, None),
    "ops.target_rotate_key": (_target_rotate_key, "target.rotate_key"),
    "ops.source_set_secret": (_source_set_secret, "source.set_secret"),
    "ops.source_recheck": (_source_recheck, None),
    "ops.source_reconnect": (_source_reconnect, None),
    "ops.edition_set_lang": (_edition_set_lang, None),
    "ops.edition_set_series_name": (_edition_set_series_name, None),
    "ops.restore_adopt": (_restore_adopt, "reconcile.confirm_deletion"),
    "ops.tombstone_accept_lost_keyring": (_accept_lost_keyring, "tombstone.accept_lost_keyring"),
    "ops.secrets_import": (_secrets_import, "secrets.import"),
    "ops.tombstone_set_aside_journal": (_set_aside_journal, "tombstone.set_aside_journal"),
    "ops.tombstone_rekey": (_tombstone_rekey, "tombstone.rekey"),
    "ops.tombstone_flush": (_tombstone_flush, None),
    "ops.adopt_set_follow_from": (_set_follow_from, None),
    "ops.adopt_set_coverage": (_set_coverage, None),
    "ops.adopt_confirm_migration": (_confirm_migration, None),
    "ops.pair_propose": (_pair_propose, None),
    "ops.pair_confirm": (_pair_confirm, None),
    "ops.pair_auto": (_pair_auto, None),
    "ops.pair_decline": (_pair_decline, None),
    "ops.migration_report": (_migration_report, None),
    "ops.repatriate_plan": (_repatriate_plan, None),
    "ops.repatriate_apply": (_repatriate_apply, None),
    "ops.repatriate_check": (_repatriate_check, None),
    "ops.detach_restore_upstream": (_give_back, None),
    "ops.purge": (_purge, "purge.now"),
    "ops.kept_ack": (_kept_ack, "purge.now"),
    "ops.purge_checkpoint": (_settle_log, None),
    "ops.leftovers_plan": (_leftovers_plan, None),
    "ops.leftovers_apply": (_leftovers_apply, "purge.now"),
    "ops.probe": (_probe, None),
    "ops.withdraw_plan": (_withdraw_plan, None),
    "ops.withdraw_apply": (_withdraw_apply, "shelf.withdraw"),
    "ops.shelf_purge_upstream": (_shelf_purge_upstream, "shelf.purge_upstream"),
    "ops.relink": (_relink, None),
    "ops.rate": (_rate, "rate"),
    "ops.rate_pending": (_rate_pending, None),
    "ops.rate_batch": (_rate_batch, "rate"),
    "ops.attach_list": (_attach_list, None),
    "ops.attach_detach": (_attach_detach, "rate"),
    "ops.attach_plan": (_attach_plan, None),
    "ops.attach_confirm": (_attach_confirm, "rate"),
    "ops.attach_decline": (_attach_decline, None),
    "ops.edition_merge": (_edition_merge, "edition.merge"),
    "ops.edition_merge_proposals": (_edition_merge_proposals, "edition.merge"),
    "ops.root_takeover": (_root_takeover, "root.takeover"),
    "ops.root_reinit": (_root_reinit, "root.reinit"),
    "ops.root_restored": (_root_restored, None),
    "ops.store_reinit": (_store_reinit, "store.reinit"),
    "ops.restore_acknowledge": (_restore_acknowledge, "restore.acknowledge"),
    "ops.rebaseline_inodes": (_rebaseline_inodes, None),
    "ops.intents_list": (_intents_list, None),
    "ops.intents_resolve": (_intents_resolve, "intents.resolve"),
    "ops.revisions_list": (_revisions_list, None),
    "ops.revisions_apply": (_revisions_apply, "revisions.apply"),
    "ops.revisions_force": (_revisions_force, "revisions.force"),
    "ops.normalize_migrate": (_normalize_migrate, "normalize.migrate"),
    "ops.content_reneutralize": (_content_reneutralize, None),
    "ops.meta_rebuild": (_meta_rebuild, None),
    "ops.meta_check": (_meta_check, None),
    "ops.meta_enrich": (_meta_enrich, None),
    "ops.backfill_estimate": (_backfill_estimate, None),
    "ops.backfill_start": (_backfill_start, None),
    "ops.follow": (_follow, None),
    "ops.unfollow": (_unfollow, None),
    "ops.source_search": (_source_search, None),
    "ops.source_filters": (_source_filters, None),
    "ops.acquire": (_acquire, None),
}


# While the instance is `restored_behind` (the database older than the disks, EF-INT-026) nothing
# of the operating surface writes, even before the first start that follows the restore has frozen
# the roots; what stays: reading, the exits of the restore, and `secrets import` (its /data half).
READS_AND_RESTORE_EXITS = frozenset(
    {
        "ops.status", "ops.missing_list", "ops.jobs_list", "ops.pair_propose",
        "ops.migration_report", "ops.repatriate_plan", "ops.repatriate_check",
        "ops.intents_list", "ops.withdraw_plan", "ops.backfill_estimate", "ops.meta_check",
        "ops.rate_pending", "ops.attach_list", "ops.source_search", "ops.source_filters",
        "ops.restore_adopt", "ops.restore_acknowledge", "ops.tombstone_set_aside_journal",
        "ops.secrets_import",
        "ops.tombstone_accept_lost_keyring",
    }
)  # fmt: skip


def run_ops_method(ops: OpsServices, principal: Principal, method: str, params: Params) -> object:
    """One call, refusals as `RpcError` (the socket) with the catalog code in `data`."""
    implementation, _reserved = OPS_METHODS[method]
    try:
        if method not in READS_AND_RESTORE_EXITS and ops.restored_behind():
            raise CodedRefusal(
                "instance.restored_behind",
                t("local_cli.restored_behind"),
            )
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
