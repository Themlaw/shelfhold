"""``shelfhold status``: warnings an operator should see (review PR #41; more in later lots)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.composition import open_fsops
from shelfhold.config import ResolvedSettings
from shelfhold.export.application.restore import RestoreGuard
from shelfhold.export.application.roots import load_roots
from shelfhold.export.domain.path_map import PathMap
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.export.domain.restore import RestoreStatus
from shelfhold.headless.applied import applied_public_url
from shelfhold.i18n import t
from shelfhold.identity.public import (
    IDENTITY_HEADER_AUDIT,
    RelyingParty,
    local_auth,
    relying_party,
)
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import current_revision, head_revision
from shelfhold.infrastructure.observability.network_fs import REFUSED_KINDS, filesystem_kind
from shelfhold.infrastructure.schema import acquisition, platform
from shelfhold.infrastructure.secrets.diagnostics import collect_warnings
from shelfhold.infrastructure.secrets.errors import KeyringError
from shelfhold.infrastructure.volumes import data_dir_writable_by_others
from shelfhold.integrations.apprise import operating_bus
from shelfhold.interfaces.cli.secrets import warning_text
from shelfhold.ops.backup import backup_warnings
from shelfhold.ops.wiring import open_ops

BACKUP_TEXT: dict[str, Callable[[str], str]] = {
    "backup_not_configured": lambda detail: t("status.backup_not_configured", detail=detail),
    "backup_incomplete": lambda detail: t("status.backup_incomplete", detail=detail),
    "backup_passphrase_colocated": lambda detail: t(
        "status.backup_passphrase_colocated", detail=detail
    ),
    "backup_same_device": lambda detail: t("status.backup_same_device", detail=detail),
    "backup_stale": lambda detail: t("status.backup_stale", detail=detail),
}


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    status = commands.add_parser("status", help=t("cli.status_help"), allow_abbrev=False)
    status.add_argument("--json", action="store_true", help=t("cli.json_help"))
    status.set_defaults(handler=run, configure_logging=False)


def restored_behind_warning(status: RestoreStatus) -> dict[str, str] | None:
    """EF-INT-026: the instance is read only, and this is the way out."""
    if not status.behind:
        return None
    return {
        "code": "restored_behind",
        "message": t("status.restored_behind", command=status.exit_command),
    }


def mismatch_warnings(status: RestoreStatus) -> list[dict[str, str]]:
    """A root whose sentinel is forged, unreadable or out of bounds is not trusted and not
    frozen: it is left alone, and the operator is told what to fix."""
    return [
        {
            "code": "root_sentinel_mismatch",
            "message": t("status.root_sentinel_mismatch", path=finding.path),
        }
        for finding in status.findings
        if finding.state is RootState.MISMATCH
    ]


def _restore_status(resolved: ResolvedSettings) -> RestoreStatus | None:
    """Read only: the sentinels against the database, without taking the instance lock."""
    settings = resolved.values
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
            if not paths:
                return None
            loaded = load_roots(connection, PathMap.same_path(paths))
        fsops = open_fsops(loaded, umask=settings.umask)
        return RestoreGuard(
            engine, fsops, clock=SystemClock(), bus=operating_bus(SystemClock())
        ).assess()
    except (sa.exc.SQLAlchemyError, OSError):
        return None
    finally:
        engine.dispose()


def _environment_warnings(resolved: ResolvedSettings) -> list[dict[str, str]]:
    settings = resolved.values
    found: list[dict[str, str]] = []
    if data_dir_writable_by_others(settings.data_dir):
        found.append(
            {
                "code": "data_dir_writable_by_others",
                "message": t("status.data_dir_writable_by_others", path=settings.data_dir),
            }
        )
    if settings.allow_network_data_dir:
        kind = filesystem_kind(settings.data_dir)
        if kind in REFUSED_KINDS:
            found.append(
                {
                    "code": "data_dir_on_network_filesystem",
                    "message": t(
                        "status.data_dir_on_network_filesystem", path=settings.data_dir, kind=kind
                    ),
                }
            )
    if settings.cli_allow_host:  # EF-USR-024: the host's Shelfhold uid is an admin
        found.append({"code": "cli_allow_host", "message": t("status.cli_allow_host")})
    return found


def _keyring_warnings(resolved: ResolvedSettings) -> list[dict[str, str]]:
    """ENF-SEC-005: what the keyring says to the admin, first of all a master key that lives in
    `/data` (the host's copy of `/data` then holds the key next to the secrets). A keyring that is
    not there yet is created by the first start: not a warning here. The key is never read out."""
    try:
        found = collect_warnings(resolved.values)
    except (KeyringError, OSError):
        return [{"code": "keyring_unreadable", "message": t("status.keyring_unreadable")}]
    if any(w.code == "keyring_absent" for w in found):
        return []  # nothing to warn about yet: the first start creates it
    return [{"code": f"keyring.{w.code}", "message": warning_text(w)} for w in found]


def _roles_unverified(engine: Engine) -> bool:
    table = acquisition.source_config
    with engine.connect() as connection:
        settings = connection.execute(
            sa.select(table.c.settings).where(table.c.plugin_id == "suwayomi")
        ).scalar_one_or_none()
    return bool(isinstance(settings, dict) and settings.get("roles_unverified"))


def warnings(resolved: ResolvedSettings) -> list[dict[str, str]]:
    settings = resolved.values
    database = settings.database_path
    if not database.exists() or current_revision(database) != head_revision():
        return [
            *_environment_warnings(resolved),
            *_keyring_warnings(resolved),
            {"code": "database_not_migrated", "message": t("status.database_not_migrated")},
        ]
    engine = create_db_engine(database)
    try:
        auth = local_auth(engine, settings)
        headers = auth.recent_audit_targets(IDENTITY_HEADER_AUDIT)
        applied = applied_public_url(engine)
        current = relying_party(settings.public_url)
        unverified = _roles_unverified(engine)
        saved = backup_warnings(engine, settings, SystemClock().now())
        new_passkeys = auth.unacknowledged_passkeys()
        stale_rps = auth.stale_passkey_rp_ids(
            current.id if isinstance(current, RelyingParty) else None
        )
    finally:
        engine.dispose()
    pending: list[dict[str, str]] = []
    if applied is not None and applied != (settings.public_url or ""):
        pending.append({"code": "public_url_restart", "message": t("status.public_url_restart")})
    if new_passkeys:  # owner decision 2026-11-12: until the admin acknowledges them
        shown = ", ".join(f"{name!r} ({user})" for _id, user, name, _at in new_passkeys[:10])
        pending.append(
            {
                "code": "passkey_registered",
                "message": t("status.passkey_registered", count=len(new_passkeys), items=shown),
            }
        )
    for rp_id in stale_rps:  # ADR-0050: a change of domain strands the passkeys
        pending.append(
            {
                "code": "passkey_rp_changed",
                "message": t("status.passkey_rp_changed", old=rp_id),
            }
        )
    if unverified:  # D1: the roles of the Suwayomi service account could not be read
        pending.append({"code": "roles_unverified", "message": t("status.roles_unverified")})
    for item in saved:  # EF-INT-015: durable, until the cause is gone
        pending.append(
            {
                "code": item["code"],
                "message": BACKUP_TEXT[item["code"]](item["detail"]),
            }
        )
    restore = _restore_status(resolved)
    behind = restored_behind_warning(restore) if restore is not None else None
    mismatched = mismatch_warnings(restore) if restore is not None else []
    return (
        _environment_warnings(resolved)
        + _keyring_warnings(resolved)
        + pending
        + ([behind] if behind else [])
        + mismatched
        + [
            {
                "code": "identity_header_stripped",
                "message": t("status.identity_header_stripped", header=header),
            }
            for header in headers
        ]
    )


def _waiting(resolved: ResolvedSettings) -> dict[str, object]:
    """What waits for a human, with the command that leaves each wait (EF-INT-023). Read only,
    from the database: no lock, no keyring is created."""
    database = resolved.values.database_path
    if not database.exists() or current_revision(database) != head_revision():
        return {
            "waiting": [], "queues": [], "stale_series": [], "tombstoned_upstream": [],
            "capabilities": [], "known_bad": {}, "service_urls": [], "unverifiable_dirs": [],
            "content_error_candidates": [], "jurisdiction": None, "rights_warnings": [],
            "kept_in_service": [], "wal_pending": [],
            "koreader_resets": [],
        }  # fmt: skip
    engine = create_db_engine(database)
    try:
        return open_ops(resolved.values, engine=engine, mount=False).status().as_dict()
    finally:
        engine.dispose()


def report(resolved: ResolvedSettings) -> dict[str, object]:
    """What `status --json` prints: the warnings and what waits (read by the diagnostic bundle,
    which keeps only the codes and the counts)."""
    return {"warnings": warnings(resolved), **_waiting(resolved)}


def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    found = warnings(resolved)
    ops = _waiting(resolved)
    if args.json:
        print(json.dumps({"warnings": found, **ops}, ensure_ascii=False, indent=2))
        return 0
    where: dict[str, Any] | None = ops.get("jurisdiction")  # type: ignore[assignment]
    unconfirmed = where is not None and not where["confirmed"]
    if not found and not ops["waiting"] and not unconfirmed:
        print(t("status.no_warning"))
    for warning in found:
        print(t("status.warning_line", message=warning["message"]))
    for item in ops["waiting"]:  # type: ignore[attr-defined]
        print(t("status.waiting_line", **item))
    for line in report_lines(ops):
        print(line)
    return 0


def _yes_no(value: object) -> str:
    if value is None:
        return t("status.unknown")
    return t("status.yes") if value else t("status.no")


def report_lines(ops: dict[str, object]) -> list[str]:
    """The sections of `status` that are not a wait: silent series, series buried upstream, what
    each root can do, and the age of the `known_bad_versions` list."""
    lines: list[str] = []
    for s in ops.get("stale_series", []):  # type: ignore[attr-defined]
        lines.append(t("status.stale_series_line", **s))
    for s in ops.get("content_error_candidates", []):  # type: ignore[attr-defined]
        lines.append(t("status.irreplaceable_candidate_line", **s))
    where: dict[str, Any] | None = ops.get("jurisdiction")  # type: ignore[assignment]
    if where is not None and not where["confirmed"]:  # EF-SRC-024: deduced, never asked yet
        lines.append(t("status.jurisdiction_unconfirmed", country=where["jurisdiction"]))
    for w in ops.get("rights_warnings", []):  # type: ignore[attr-defined]
        if w["reason"] == "translation_may_be_protected":
            lines.append(
                t("status.rights_translation_may_be_protected", edition_id=w["edition_id"])
            )
        else:
            lines.append(t("status.rights_may_be_protected", edition_id=w["edition_id"]))
    for u in ops.get("koreader_resets", []):  # type: ignore[attr-defined]
        lines.append(t("status.koreader_reset_line", unit_id=u["unit_id"], at=u["at"]))
    for s in ops.get("tombstoned_upstream", []):  # type: ignore[attr-defined]
        lines.append(t("status.tombstoned_upstream_line", **s))
    for w in ops.get("wal_pending", []):  # type: ignore[attr-defined]
        lines.append(t("status.wal_pending_line", **w))
    for k in ops.get("kept_in_service", []):  # type: ignore[attr-defined]
        lines.append(t("status.kept_in_service_line", **k))
    for c in ops.get("capabilities", []):  # type: ignore[attr-defined]
        if c.get("unprobed"):
            lines.append(t("status.capabilities_unprobed", root=c["root"], why=c["unprobed"]))
            continue
        link = {
            "yes": t("status.link_yes"),
            "no": t("status.link_no"),
            "not_applicable": t("status.link_not_applicable", reason=c["store_reason"]),
        }[c["hardlink_store"]]
        lines.append(
            t(
                "status.capabilities_line", root=c["root"], filesystem=c["filesystem"] or "?",
                reflink=_yes_no(c["reflink"]), link=link, inodes=_yes_no(c["inodes_stable"]),
                fallback=t("status.copy_fallback") if c["copy_fallback"] else "",
            )
        )  # fmt: skip
    for refused in ops.get("service_urls", []):  # type: ignore[attr-defined]
        lines.append(t("status.service_url_refused", **refused))
    for unverified in ops.get("unverifiable_dirs", []):  # type: ignore[attr-defined]
        lines.append(t("status.download_dir_unverifiable", **unverified))
    known: dict[str, Any] = ops.get("known_bad") or {}  # type: ignore[assignment]
    if known:
        values = {
            "listed_on": known["listed_on"],
            "age": known["age_days"],
            "minimum": known["minimum_version"],
        }
        if known["outdated"]:
            lines.append(t("status.known_bad_outdated", **values))
        else:
            lines.append(t("status.known_bad_line", **values))
        lines.extend(
            t(
                "status.known_bad_target",
                name=target["name"],
                version=target["version"] or "?",
                advisories=t("status.list_separator").join(target["advisories"]),
            )
            for target in known["targets"]
            if target["advisories"]
        )
    return lines
