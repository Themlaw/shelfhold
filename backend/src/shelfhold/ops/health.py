"""`/healthz` with the paused queues and their reasons (D11, A7), and the Health page (M3 L15).

The page is what the administrator opens to know whether the instance is well: the state of each
source with the way out, the Units nothing could give back, the originals lost, the backup, what
waits (`withdrawal_pending`, the Kavita audit, the jurisdiction) and the value indicators.

It is read from the database and the last measures, never from a disk (ENF-OPS-014): a library
disk asleep stays asleep. It writes nothing and applies nothing (`shelf withdraw` stays a
reserved action). It reuses what exists and recomputes nothing: `collect` for the waits,
`irreplaceability` for the Units, `backup_warnings` for the backup, `compute_stats` for the
indicators. Nothing in it is a secret, a passphrase or a path: a presence is a code, never a
content.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.follow import OUTAGE_CYCLES
from shelfhold.config import Settings
from shelfhold.i18n import t
from shelfhold.infrastructure.health import HealthProbe
from shelfhold.infrastructure.schema import acquisition, platform, storage
from shelfhold.infrastructure.stats import DEFAULT_SINCE, compute_stats, parse_since
from shelfhold.library.public import irreplaceability
from shelfhold.ops.backup.run import backup_warnings
from shelfhold.ops.ports import PausedQueue, WaitingItem
from shelfhold.ops.services import OpsServices
from shelfhold.ops.status import collect, command_for, jurisdiction_report
from shelfhold.plugin_host.health import TypedState, typed_for_code

__all__ = ["Action", "OpsHealth", "action_of", "build_health_page"]

STORE_BACKUP_KEY = "store.backup_declared_at"  # what the host's own backup tool declared
LOST_ORIGINAL = "content.original_lost"  # stable code of the entry (EF-BIB-051)
KAVITA_AUDIT_STATES = ("child_exposure", "permission_denied", "target_forbidden")


@dataclass(frozen=True)
class Action:
    """What the administrator does: a command that exists, whether it is a reserved action, and
    the sentence that says it (a literal key of the catalog)."""

    command: str
    reserved: bool = False
    text: str = ""


class OpsHealth:
    """The probe of the instance, plus what waits: the names for `paused_queues`, the reason of
    each for `paused`. Read only; an unreadable database is an empty list (`/readyz` says why)."""

    def __init__(self, probe: HealthProbe, ops: Callable[[], OpsServices]) -> None:
        self._probe = probe
        self._ops = ops

    def migration_progress(self) -> tuple[int, int] | None:
        return self._probe.migration_progress()

    def readiness(self) -> Mapping[str, bool]:
        return self._probe.readiness()

    def _queues(self) -> list[PausedQueue]:
        try:
            return self._ops().paused_queues()
        except sa.exc.SQLAlchemyError:
            return []

    def paused_queues(self) -> list[str]:
        return [queue.queue for queue in self._queues()]

    def paused(self) -> list[dict[str, str]]:
        return [{"queue": q.queue, "reason": q.reason} for q in self._queues()]


# -- the page -------------------------------------------------------------------------------------

# Literal keys, one by code: a key that is built is a key nobody finds when it is renamed.
_ACTION_TEXT: dict[str, Callable[[], str]] = {
    "source.error": lambda: t("errors.source.error.action"),
    "source.broken": lambda: t("errors.source.broken.action"),
    "source.auth_required": lambda: t("errors.source.auth_required.action"),
    "source.auth_expired": lambda: t("errors.source.auth_expired.action"),
    "source.rate_limited": lambda: t("errors.source.rate_limited.action"),
    "source.content_removed": lambda: t("errors.source.content_removed.action"),
    "source.changed": lambda: t("errors.source.changed.action"),
    "source.temporarily_unavailable": lambda: t("errors.source.temporarily_unavailable.action"),
    "source.version_unsupported": lambda: t("errors.source.version_unsupported.action"),
    "source.not_configured": lambda: t("errors.source.not_configured.action"),
    "source.not_loaded": lambda: t("errors.source.not_loaded.action"),
    "source.auth_mode_unsupported": lambda: t("errors.source.auth_mode_unsupported.action"),
    "source.instance_changed": lambda: t("errors.source.instance_changed.action"),
    "source.ref_has_secret": lambda: t("errors.source.ref_has_secret.action"),
}
_BACKUP_COMMAND = {
    "backup_not_configured": "shelfhold config set",
    "backup_incomplete": "shelfhold config set",
    "backup_passphrase_colocated": "shelfhold backup run",
    "backup_same_device": "shelfhold config set",
    "backup_stale": "shelfhold backup run",
}
_BACKUP_TEXT: dict[str, Callable[[], str]] = {
    "backup_not_configured": lambda: t("health.backup_not_configured"),
    "backup_incomplete": lambda: t("health.backup_incomplete"),
    "backup_passphrase_colocated": lambda: t("health.backup_passphrase_colocated"),
    "backup_same_device": lambda: t("health.backup_same_device"),
    "backup_stale": lambda: t("health.backup_stale"),
}


def action_of(typed: TypedState) -> Action:
    """The way out of a typed state: the command `status` gives for its waiting state, else the
    bare command the table names."""
    if typed.waiting_state:
        command, reserved = command_for(typed.waiting_state, "")
    else:
        command, reserved = f"shelfhold {typed.command}", False
    text = _ACTION_TEXT.get(typed.code)
    return Action(command, reserved, text() if text else "")


def _source_state(health: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    """`(state, code, since)` from a health document: a pause first (its own code), then an
    outage of `OUTAGE_CYCLES` cycles in a row (`source.broken`, announced by the follow cycle),
    then a source that is not loaded."""
    pause = health.get("pause")
    if isinstance(pause, dict):
        return "paused", str(pause.get("event_type", "source.error")), pause.get("since")
    if health.get("unavailable") or int(health.get("outage") or 0) >= OUTAGE_CYCLES:
        return "degraded", "source.broken", None
    if health.get("not_loaded"):
        return "degraded", "source.not_loaded", None
    return "ok", None, None


def _sources(connection: Connection) -> list[dict[str, object]]:
    config = acquisition.source_config
    rows = connection.execute(
        sa.select(config.c.plugin_id, config.c.health).order_by(config.c.plugin_id)
    )
    found: list[dict[str, object]] = []
    for row in rows:
        health = row.health if isinstance(row.health, dict) else {}
        state, code, since = _source_state(health)
        entry: dict[str, object] = {"plugin_id": row.plugin_id, "state": state, "code": code}
        entry["since"] = since
        entry["action"] = asdict(action_of(typed_for_code(code))) if code else None
        found.append(entry)
    return found


def _store_backup(connection: Connection, now: datetime) -> dict[str, object]:
    declared = connection.execute(
        sa.select(platform.app_setting.c.value).where(
            platform.app_setting.c.key == STORE_BACKUP_KEY
        )
    ).scalar()
    if declared is None:
        return {"declared": False, "age_days": None}
    try:
        at = datetime.fromisoformat(declared)
    except ValueError:
        return {"declared": False, "age_days": None}
    at = at if at.tzinfo else at.replace(tzinfo=UTC)
    return {"declared": True, "age_days": max((now - at).days, 0)}


def _irreplaceable(connection: Connection, now: datetime) -> dict[str, object]:
    """Counted by `irreplaceability`, Unit by Unit: the calculation is the one of EF-BIB-031 and
    of the registry of copies (EF-BIB-045), asked and not repeated."""
    from shelfhold.infrastructure.schema import catalog

    count = bare = single = 0
    for unit_id in connection.execute(sa.select(catalog.unit.c.id)).scalars().all():
        verdict = irreplaceability(connection, unit_id)
        if not verdict.irreplaceable:
            continue
        count += 1
        bare += verdict.independent_copies == 0
        single += verdict.independent_copies == 1
    return {
        "count": count,
        "without_kept_copy": bare,
        "single_copy": single,
        "store_backup": _store_backup(connection, now),
    }


def _lost_originals(connection: Connection) -> list[dict[str, object]]:
    """`untrusted` EPUB whose original is gone and whose derivative is held (EF-BIB-051 (5))."""
    content = storage.content
    rows = connection.execute(
        sa.select(content.c.id, content.c.unit_id, content.c.format, content.c.derived_hash)
        .where(content.c.store_provenance == "extracted_derived")
        .order_by(content.c.id)
    )
    action = action_of_command("shelfhold content reneutralize", t("health.original_lost_action"))
    return [
        {
            "code": LOST_ORIGINAL,
            "unit_id": r.unit_id,
            "content_id": r.id,
            "format": r.format,
            "derivative_present": r.derived_hash is not None,
            "action": action,
        }
        for r in rows
    ]


def action_of_command(command: str, text: str, *, reserved: bool = False) -> dict[str, object]:
    return asdict(Action(command, reserved, text))


def _backup(engine: Engine, settings: Settings, now: datetime) -> dict[str, object]:
    """The warnings of `status`, as entries: the presence of a passphrase in `/data` is a code and
    a date, never the phrase, its file or its folder."""
    with engine.connect() as connection:
        last = connection.execute(
            sa.select(sa.func.max(platform.backup_run.c.finished_at)).where(
                platform.backup_run.c.state == "ok"
            )
        ).scalar()
    warnings = [
        {
            "code": w["code"],
            "action": action_of_command(_BACKUP_COMMAND[w["code"]], _BACKUP_TEXT[w["code"]]()),
        }
        for w in backup_warnings(engine, settings, now)
    ]
    return {
        "last_ok_at": last.isoformat() if last else None,
        "age_hours": None if last is None else max(int((now - last).total_seconds() // 3600), 0),
        "warnings": warnings,
    }


def _waits(waiting: Sequence[WaitingItem]) -> tuple[dict[str, object], list[dict[str, object]]]:
    """D24: `withdrawal_pending` is an entry (a count and the reserved command), never applied
    by the page; the audit of the Kavita accounts is what the targets say of their exposure."""
    pending = [w for w in waiting if w.state == "withdrawal_pending"]
    command = pending[0].command if pending else command_for("withdrawal_pending", "")[0]
    withdrawal = {
        "count": len(pending),
        "action": action_of_command(command, t("health.withdrawal_action"), reserved=True),
    }
    audit: list[dict[str, object]] = [
        {
            "code": w.state,
            "subject": w.subject,
            "action": action_of_command(w.command, t("health.kavita_audit_action")),
        }
        for w in waiting
        if w.state in KAVITA_AUDIT_STATES
    ]
    return withdrawal, audit


def _jurisdiction(connection: Connection) -> dict[str, object] | None:
    report = jurisdiction_report(connection)
    if report is None:
        return None
    confirmed = bool(report["confirmed"])
    return {
        "country": report["jurisdiction"],
        "confirmed": confirmed,
        "code": None if confirmed else "rights.jurisdiction_unconfirmed",
        "action": None
        if confirmed
        else action_of_command(
            "shelfhold config set instance.jurisdiction <country>",
            t("health.jurisdiction_action"),
        ),
    }


def build_health_page(
    engine: Engine,
    settings: Settings,
    now: datetime,
    *,
    waiting: Sequence[WaitingItem] | None = None,
) -> dict[str, object]:
    """The whole page, from the database. `waiting` is what `status` lists (read here when not
    given, from the database only: no root is looked at)."""
    with engine.connect() as connection:
        items = list(waiting) if waiting is not None else collect(connection)
        withdrawal, audit = _waits(items)
        page: dict[str, object] = {
            "generated_at": now.isoformat(),
            "sources": _sources(connection),
            "irreplaceable": _irreplaceable(connection, now),
            "lost_originals": _lost_originals(connection),
            "withdrawal_pending": withdrawal,
            "kavita_audit": audit,
            "jurisdiction": _jurisdiction(connection),
        }
    page["backup"] = _backup(engine, settings, now)
    page["stats"] = compute_stats(engine, now=now, since_days=parse_since(DEFAULT_SINCE)).as_dict()
    return page
