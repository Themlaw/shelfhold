"""What waits for a human, read from the database (EF-INT-023, ENF-OPS-011).

One read per kind of wait: a paused series, a record that went missing, a job gone dead, a target
whose health document says it waits, a paused follow cycle, an Edition of unknown language. The
command that leaves each state comes from one table, so `status`, `/healthz` and the exit routes
cannot name different commands.
"""

from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.acquisition.public import stale_series
from shelfhold.export.application.target_health import TargetHealth
from shelfhold.export.domain.kavita_compat import (
    KNOWN_BAD_LISTED_ON,
    KNOWN_BAD_OUTDATED_AFTER,
    KNOWN_BAD_VERSIONS,
    MINIMUM_VERSION,
    known_bad_age,
)
from shelfhold.identity.public import EXIT_ROUTES
from shelfhold.infrastructure.schema import acquisition, catalog, export, platform
from shelfhold.ops.ports import PausedQueue, WaitingItem

__all__ = [
    "collect",
    "command_for",
    "known_bad_report",
    "queues_of",
    "stale_report",
    "tombstoned_report",
]

UNKNOWN_LANGUAGE = "x-unknown"  # what an adopted Edition without a language carries
FOREIGN_WRITER = "foreign_writer"  # `export_series.paused_reason`

# state -> the command, with `{subject}` and `{extra}` filled by the reader
_TEMPLATES = {
    "missing": "shelfhold missing resolve {extra} {subject} (--reexport | --unfollow)",
    "paused_foreign_writer": "shelfhold series resume {subject}",
    "dead_letter": "shelfhold jobs retry {subject}",
    "not_indexed": "shelfhold jobs retry {subject}",
    "instance_changed": "shelfhold target reconnect {subject}",
    "target_forbidden": "shelfhold target reconnect {subject}",
    "key_expiring": "shelfhold target rotate-key {subject} --password-file <file>",
    "auth_expired": "shelfhold source set-secret --secret-file <file>",
    "auth_mode_unsupported": "shelfhold source recheck",
    "permission_denied": "shelfhold target recheck {extra}",
    "child_exposure": "shelfhold target recheck {subject}",
    "route_missing": "shelfhold target recheck {subject}",
    "language_unknown": "shelfhold edition set-lang {subject} <lang>",
    "mount_sentinel_missing": "shelfhold target recheck {extra}",
    "series_name_collision": 'shelfhold edition set-series-name {subject} "<name>"',
    "restored_behind": "shelfhold reconcile --adopt-after-restore",
    "tombstone_key_unknown": "shelfhold tombstone accept-lost-keyring",
    "tombstone_lines_owed": "shelfhold tombstone flush",
    "adoption_refused": "shelfhold adopt --confirm-migration",
    "coverage_unknown": "shelfhold adopt set-follow-from {subject} <chapter>",
    # L42: series of the Suwayomi library the follow cycle did not file (not states of the
    # enumeration: their exit is a command that exists, not a route of the table)
    "pending_pairing": (
        "shelfhold adopt pair --series {subject} (--source-key <key> --remote-id <id> | --decline)"
    ),
    "awaiting_shelf": (
        "shelfhold shelf create <name> --max-age 18+ --target <target> --subpath <folder>"
    ),
}


def command_for(state: str, subject: str, extra: str = "") -> tuple[str, bool]:
    """`(command, reserved)`: reserved when every exit of the state is (a plain exit next to a
    reserved one, as for the restore, makes the command a plain one)."""
    template = _TEMPLATES.get(state, "shelfhold source recheck")
    reserved = any(
        r.waiting_state == state and r.reserved is not None for r in EXIT_ROUTES
    ) and not any(r.waiting_state == state and r.reserved is None for r in EXIT_ROUTES)
    return template.format(subject=subject, extra=extra), reserved


def _item(state: str, queue: str, subject: str, reason: str, extra: str = "") -> WaitingItem:
    command, reserved = command_for(state, subject, extra)
    return WaitingItem(state, queue, subject, reason, command, reserved)


def dress(item: WaitingItem) -> WaitingItem:
    """An item from a port, with the command of the table when it brings none."""
    if item.command:
        return item
    command, reserved = command_for(item.state, item.subject)
    return WaitingItem(item.state, item.queue, item.subject, item.reason, command, reserved)


def collect(connection: Connection) -> list[WaitingItem]:
    found: list[WaitingItem] = []
    found += _series(connection)
    found += _missing(connection)
    found += _jobs(connection)
    found += _targets(connection)
    found += _not_indexed(connection)
    found += _follow(connection)
    found += _languages(connection)
    found += _discovery(connection)
    return found


def _series(connection: Connection) -> list[WaitingItem]:
    series, destination = export.export_series, export.shelf_destination
    rows = connection.execute(
        sa.select(series.c.id, series.c.paused_reason, destination.c.target_id)
        .select_from(series.join(destination, destination.c.id == series.c.shelf_destination_id))
        .where(series.c.paused_reason.isnot(None))
        .order_by(series.c.id)
    )
    items: list[WaitingItem] = []
    for row in rows:
        if row.paused_reason == FOREIGN_WRITER:
            items.append(
                _item("paused_foreign_writer", "export", row.id, "foreign writer detected")
            )
        elif row.paused_reason == "permission_denied":
            items.append(
                _item("permission_denied", "export", row.id, "write refused", row.target_id)
            )
    return items


def _missing(connection: Connection) -> list[WaitingItem]:
    record = export.export_record
    rows = connection.execute(
        sa.select(record.c.shelf_destination_id, record.c.export_item_id, record.c.rel_path)
        .where(record.c.state == "missing")
        .order_by(record.c.export_item_id)
    )
    return [
        _item(
            "missing",
            "export",
            row.export_item_id,
            f"{row.rel_path} is gone",
            row.shelf_destination_id,
        )
        for row in rows
    ]


def _jobs(connection: Connection) -> list[WaitingItem]:
    job = platform.job_view
    rows = connection.execute(
        sa.select(job.c.id, job.c.kind, job.c.error_code, job.c.last_error)
        .where(job.c.status == "dead_letter")
        .order_by(job.c.id)
    )
    return [
        _item("dead_letter", row.kind, row.id, row.error_code or row.last_error or "gave up")
        for row in rows
    ]


def _targets(connection: Connection) -> list[WaitingItem]:
    target = export.export_target
    items: list[WaitingItem] = []
    shelf_exposed = bool(
        connection.execute(
            sa.select(sa.func.count())
            .select_from(export.shelf)
            .where(export.shelf.c.paused_reason == "child_exposure")
        ).scalar_one()
    )
    for row in connection.execute(sa.select(target.c.id, target.c.name, target.c.health)):
        health = TargetHealth.from_json(row.health) if row.health else TargetHealth()
        state = health.waiting_state.value if health.waiting_state else None
        if state == "not_indexed":
            state = None  # read from the records (`_not_indexed`), the source of truth
        if state is None and shelf_exposed and not items:
            state = "child_exposure"
        if state is not None:
            reason = {
                "target_forbidden": f"403 on {health.paused_route}",
                "route_missing": ", ".join(health.missing_routes),
                "key_expiring": f"key expires {health.key_expires_at}",
                "not_indexed": "a written file was never indexed",
            }.get(state, state)
            items.append(_item(state, "export", row.id, reason or state))
    return items


def _not_indexed(connection: Connection) -> list[WaitingItem]:
    record = export.export_record
    rows = connection.execute(
        sa.select(record.c.export_item_id, record.c.rel_path)
        .where(record.c.error_code == "target.not_indexed")
        .order_by(record.c.export_item_id)
    )
    return [
        _item("not_indexed", "export", row.export_item_id, f"{row.rel_path} was never indexed")
        for row in rows
    ]


def _follow(connection: Connection) -> list[WaitingItem]:
    config = acquisition.source_config
    rows = connection.execute(sa.select(config.c.plugin_id, config.c.health))
    items: list[WaitingItem] = []
    for row in rows:
        pause = (row.health or {}).get("pause")
        if not isinstance(pause, dict):
            continue
        state = str(pause.get("waiting_state") or pause.get("failure_class") or "source_paused")
        items.append(_item(state, "follow", row.plugin_id, str(pause.get("failure_class", state))))
    return items


def _discovery(connection: Connection) -> list[WaitingItem]:
    """Series of the library the follow cycle leaves alone until the admin acts (L42)."""
    notice = acquisition.follow_notice
    rows = connection.execute(
        sa.select(notice.c.kind, notice.c.subject)
        .where(notice.c.kind.in_(("pending_pairing", "awaiting_shelf")))
        .order_by(notice.c.kind, notice.c.subject)
    )
    items: list[WaitingItem] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if (row.kind, row.subject) in seen:  # one adopted series, several near matches: one line
            continue
        seen.add((row.kind, row.subject))
        if row.kind == "pending_pairing":
            reason = "a series of the library looks like this adopted series"
            items.append(_item("pending_pairing", "catalog", row.subject, reason))
        else:
            items.append(_item("awaiting_shelf", "catalog", "-", "no adult shelf to file them on"))
    return items


def _languages(connection: Connection) -> list[WaitingItem]:
    edition = catalog.edition
    rows = connection.execute(
        sa.select(edition.c.id).where(edition.c.lang == UNKNOWN_LANGUAGE).order_by(edition.c.id)
    )
    return [
        _item("language_unknown", "catalog", row.id, "the language is not known") for row in rows
    ]


def queues_of(connection: Connection, items: list[WaitingItem]) -> list[PausedQueue]:
    """The paused queues: the queue of each waiting item (reason: its state), and the job kinds
    paused by the window (they resume by themselves)."""
    queues: dict[str, PausedQueue] = {}
    for item in items:
        queues.setdefault(item.queue, PausedQueue(item.queue, item.state, item.command))
    job = platform.job_view
    for row in connection.execute(
        sa.select(job.c.kind, job.c.error_code).where(job.c.status == "paused").order_by(job.c.kind)
    ):
        queues.setdefault(row.kind, PausedQueue(row.kind, row.error_code or "paused"))
    return sorted(queues.values(), key=lambda q: q.queue)


def stale_report(connection: Connection, now: datetime) -> list[dict[str, object]]:
    """The series that went silent (EF-SRC-026 c, `series.stale` as it stands now): k = 3 times the
    observed cadence, 14 days for a series without one (the M1a defaults)."""
    return [
        {
            "edition_id": s.edition_id,
            "title": s.title,
            "last_discovery": s.last_discovery.isoformat(),
            "silent_days": max((now - s.last_discovery).days, 0),
            "threshold_days": s.threshold.days,
        }
        for s in stale_series(connection, now)
    ]


def tombstoned_report(connection: Connection) -> list[dict[str, object]]:
    """The series of the Suwayomi library that a purge buried and the follow cycle leaves alone
    (EF-BIB-053 (2)): the admin removes them from Suwayomi by hand (M1a)."""
    notice = acquisition.follow_notice
    rows = connection.execute(
        sa.select(notice.c.subject, notice.c.told_at)
        .where(notice.c.kind == "tombstoned")
        .order_by(notice.c.told_at, notice.c.key_hash)
    )
    return [{"title": r.subject, "since": r.told_at.isoformat()} for r in rows]


def known_bad_report(connection: Connection, today: date) -> dict[str, object]:
    """The `known_bad_versions` list shipped with the release, its age, and the Kavita versions
    of the targets it applies to (ENF-COMP-001)."""
    age = known_bad_age(today)
    targets: list[dict[str, object]] = []
    for row in connection.execute(
        sa.select(export.export_target.c.id, export.export_target.c.name,
                  export.export_target.c.health).order_by(export.export_target.c.id)
    ):  # fmt: skip
        health = TargetHealth.from_json(row.health) if row.health else TargetHealth()
        targets.append(
            {"target": row.id, "name": row.name, "version": health.version,
             "advisories": list(health.warnings)}
        )  # fmt: skip
    return {
        "listed_on": KNOWN_BAD_LISTED_ON.isoformat(),
        "age_days": age.days,
        "outdated": age > KNOWN_BAD_OUTDATED_AFTER,
        "minimum_version": str(MINIMUM_VERSION),
        "entries": [
            {"advisory": k.advisory, "category": k.category, "below": str(k.below)}
            for k in KNOWN_BAD_VERSIONS
        ],
        "targets": targets,
    }


def service_url_report(connection: Connection) -> list[dict[str, object]]:
    """A service address (Suwayomi) the URL rule refuses now: no request leaves for it, and the
    administrator is told which address and why (ENF-SEC-001). Fixed by `source add suwayomi`."""
    from shelfhold.headless.urls import service_url_problem

    found: list[dict[str, object]] = []
    for settings in connection.execute(
        sa.select(acquisition.source_config.c.settings).where(
            acquisition.source_config.c.plugin_id == "suwayomi"
        )
    ).scalars():
        url = (settings or {}).get("base_url")
        problem = service_url_problem(str(url)) if url else None
        if problem is not None:
            found.append(
                {"service": "suwayomi", "url": _without_userinfo(str(url)), "reason": problem}
            )
    return found


def _without_userinfo(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}{':' + str(parts.port) if parts.port else ''}{parts.path}"
