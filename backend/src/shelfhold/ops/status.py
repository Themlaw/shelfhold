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
from shelfhold.catalog.public import RatingService, RightsService
from shelfhold.export.application.library_repair import barrier_closed_for
from shelfhold.export.application.target_health import TargetHealth
from shelfhold.export.application.to_rate import to_rate
from shelfhold.export.application.withdrawal import build_plan
from shelfhold.export.domain.kavita_compat import (
    KNOWN_BAD_LISTED_ON,
    KNOWN_BAD_OUTDATED_AFTER,
    KNOWN_BAD_VERSIONS,
    MINIMUM_VERSION,
    known_bad_age,
)
from shelfhold.identity.public import EXIT_ROUTES
from shelfhold.infrastructure.schema import acquisition, catalog, export, platform
from shelfhold.infrastructure.wal import WAL_PENDING_KEY
from shelfhold.library.public import (
    KEPT_PREFIX,
    backlog_row,
    content_error_candidates,
    held_for_irreplaceable_units,
)
from shelfhold.ops.ports import PausedQueue, WaitingItem

__all__ = [
    "candidate_report",
    "collect",
    "command_for",
    "jurisdiction_report",
    "kept_in_service_report",
    "known_bad_report",
    "koreader_reset_report",
    "queues_of",
    "rights_report",
    "stale_report",
    "tombstoned_report",
    "wal_pending_report",
]

UNKNOWN_LANGUAGE = "x-unknown"  # what an adopted Edition without a language carries
FOREIGN_WRITER = "foreign_writer"  # `export_series.paused_reason`

# state -> the command, with `{subject}` and `{extra}` filled by the reader
_TEMPLATES = {
    "maintenance": "shelfhold maintenance off",
    "missing": "shelfhold missing resolve {extra} {subject} (--reexport | --unfollow)",
    "paused_foreign_writer": "shelfhold series resume {subject}",
    "dead_letter": "shelfhold jobs retry {subject}",
    "safeguard_backlog": "shelfhold jobs list",
    "not_indexed": "shelfhold jobs retry {subject}",
    "instance_changed": "shelfhold target reconnect {subject}",
    "target_forbidden": "shelfhold target reconnect {subject}",
    "key_expiring": "shelfhold target rotate-key {subject} --password-file <file>",
    "auth_expired": "shelfhold source set-secret --secret-file <file>",
    "source_instance_changed": "shelfhold source reconnect",
    "auth_mode_unsupported": "shelfhold source recheck",
    "permission_denied": "shelfhold target recheck {extra}",
    "child_exposure": "shelfhold target recheck {subject}",
    "library_type_changed": "shelfhold target reconnect {subject}",  # EF-EXP-049
    # M2 L09: the age gate of an EPUB series (not states of the enumeration: the next export
    # run reads Kavita again and lifts them; the recheck says what Kavita answers)
    "series_age_stale": "shelfhold target recheck {extra}",
    "series_split": "shelfhold target recheck {extra}",
    "route_missing": "shelfhold target recheck {subject}",
    "language_unknown": "shelfhold edition set-lang {subject} <lang>",
    "mount_sentinel_missing": "shelfhold target recheck {extra}",
    "series_name_collision": 'shelfhold edition set-series-name {subject} "<name>"',
    "withdrawal_pending": "shelfhold shelf withdraw",
    "rating_pending": "shelfhold rate <age> --work {subject} (all at once: --pending)",
    "follow_suspended": "shelfhold rate <age> --work {subject} (all at once: --pending)",
    "restored_behind": "shelfhold reconcile --adopt-after-restore",
    "restored_credentials": "shelfhold restore acknowledge",
    "tombstone_key_unknown": "shelfhold secrets import --passphrase-file <file> --in <file>",
    "tombstone_lines_owed": "shelfhold tombstone flush",
    "tombstone_journal_truncated": "shelfhold tombstone set-aside-journal --root {subject}",
    "adoption_refused": "shelfhold adopt --confirm-migration",
    "coverage_unknown": "shelfhold adopt set-follow-from {subject} <chapter>",
    # L42: series of the Suwayomi library the follow cycle did not file (not states of the
    # enumeration: their exit is a command that exists, not a route of the table)
    "pending_pairing": (
        "shelfhold adopt pair --series {subject} (--source-key <key> --remote-id <id> | --decline)"
    ),
    # L25: the bin past 30 days keeps what became irreplaceable; the exit is the restore (or the
    # purge, which asks for the word)
    "bin_held_irreplaceable": (
        "POST /api/v1/units/{subject}/restore (a fresh admin session), "
        "or shelfhold purge --now --unit {subject}"
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
    found += _withdrawals(connection)
    found += _integrity_blocked(connection)
    found += _to_rate(connection)
    found += _safeguard_backlog(connection)
    found += _held_bins(connection)
    found += _maintenance(connection)
    return found


def _held_bins(connection: Connection) -> list[WaitingItem]:
    return [
        _item(
            "bin_held_irreplaceable",
            "library",
            unit,
            f"{count} bin entries kept because the Unit is irreplaceable",
        )
        for unit, count in sorted(held_for_irreplaceable_units(connection).items())
    ]


def _maintenance(connection: Connection) -> list[WaitingItem]:
    """The mode freezes every write until an admin leaves it (EF-INT-018): `status` says so, with
    the command, for as long as it lasts, however long ago it was entered."""
    since = connection.execute(sa.select(platform.instance.c.maintenance_since)).scalar()
    if since is None:
        return []
    return [_item("maintenance", "instance", "instance", f"maintenance since {since.isoformat()}")]


def _safeguard_backlog(connection: Connection) -> list[WaitingItem]:
    """Units owed a copy in /store that the last cycle could not take (200 Units, 5 GiB or the
    room of the volume): they wait for the next window; `jobs list` shows the job (EF-BIB-042)."""
    left = backlog_row(connection)
    if left is None:
        return []
    units, size = left
    return [_item("safeguard_backlog", "store", "store", f"{units} unit(s), {size} byte(s)")]


def _to_rate(connection: Connection) -> list[WaitingItem]:
    """Unrated Works that wait in "À classer": suspended with an adopted child library, or held
    out of a shelf below 18+. `rate` is the exit of both, a reserved action (EF-EXP-048)."""
    return [
        _item(i.state, "catalog", i.work_id, f"{i.title}: {i.reason}") for i in to_rate(connection)
    ]


def _withdrawals(connection: Connection) -> list[WaitingItem]:
    """Files that a shelf below 18+ must not hold and that nobody has taken out yet: the plan of
    the withdrawal is not empty. Its only exit is `shelf withdraw`, a reserved action, so a file
    that was refused (a rating raised, a ceiling lowered) shows here until an admin applies it."""
    family = connection.execute(
        sa.select(export.shelf.c.id).where(export.shelf.c.max_age_rating < 18).limit(1)
    ).first()
    if family is None:
        return []
    plan = build_plan(connection, RatingService())
    return [
        _item("withdrawal_pending", "export", i.edition_id, f"{i.reason}: {len(i.files)} file(s)")
        for i in plan.items
    ]


def _integrity_blocked(connection: Connection) -> list[WaitingItem]:
    """A Unit whose served file is damaged and whose repair waits because the barrier of its shelf
    is closed (the Edition is no longer admitted, EF-BIB-037): replacing the file would make it
    readable again. The exit is the withdrawal (`shelf withdraw`, a reserved action); the fresh
    bytes wait outside the library (EF-BIB-012)."""
    unit = catalog.unit
    found: list[WaitingItem] = []
    for row in connection.execute(
        sa.select(unit.c.id, unit.c.edition_id)
        .where(unit.c.integrity_state == "corrupt")
        .order_by(unit.c.id)
    ):
        why = barrier_closed_for(connection, row.id)
        if why is not None:
            found.append(
                _item(
                    "withdrawal_pending",
                    "export",
                    row.edition_id,
                    f"integrity_blocked_by_{why}: unit {row.id}",
                )
            )
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
        elif row.paused_reason == "series_age_stale":  # export.series_age_stale (M2 L09)
            items.append(
                _item(
                    "series_age_stale", "export", row.id,
                    "export.series_age_stale: Kavita shows an age below the rated one",
                    row.target_id,
                )
            )  # fmt: skip
        elif row.paused_reason == "series_split":
            items.append(
                _item(
                    "series_split", "export", row.id,
                    "Kavita split the series: its age is not locked on every part",
                    row.target_id,
                )
            )  # fmt: skip
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
                "library_type_changed": "a library type changed in Kavita; the shelf is paused",
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


def candidate_report(connection: Connection) -> list[dict[str, object]]:
    """Units that became irreplaceable candidates after repeated content errors of their upstream
    source (EF-BIB-042 (3)). A list to read: nothing is copied or removed because of it."""
    return [
        {
            "unit_id": c.unit_id,
            "source_key": c.source_key,
            "errors": c.errors,
            "last_error": c.last_error_code or "",
            "independent_copies": c.independent_copies,
        }
        for c in content_error_candidates(connection)
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


def jurisdiction_report(connection: Connection) -> dict[str, object] | None:
    """The country of the instance and whether the admin confirmed it (EF-SRC-024, D4): until
    `config set instance.jurisdiction`, the value is the one the locale suggests. `None` before
    the instance exists. The calculation uses the unconfirmed value."""
    if connection.execute(sa.select(platform.instance.c.instance_id)).first() is None:
        return None
    country, confirmed = RightsService(connection).jurisdiction()
    return {"jurisdiction": country, "confirmed": confirmed}


def koreader_reset_report(connection: Connection, since: datetime) -> list[dict[str, object]]:
    """The files replaced at their path since `since` (EF-EXP-016, D21): Kavita kept its progress,
    KOReader starts each of them again. The Activity screen comes in M3; until then `status`
    lists them, newest first."""
    table = export.export_upgrade
    rows = connection.execute(
        sa.select(table.c.unit_id, table.c.reason, table.c.automatic, table.c.created_at)
        .where(table.c.koreader_reset.is_(True), table.c.created_at >= since)
        .order_by(table.c.created_at.desc(), table.c.id.desc())
    )
    found = [
        {
            "unit_id": r.unit_id,
            "reason": r.reason,
            "automatic": bool(r.automatic),
            "at": r.created_at.isoformat(),
        }
        for r in rows
    ]
    # a damaged library file replaced at its path (EF-BIB-012) has no exported item: the event
    # is the entry
    event = platform.event
    repairs = connection.execute(
        sa.select(event.c.payload, event.c.occurred_at).where(
            event.c.type == "unit.upgraded",
            event.c.occurred_at >= since,
            sa.func.json_extract(event.c.payload, "$.upgrade_id").is_(None),
            sa.func.json_extract(event.c.payload, "$.koreader_reset") == 1,
        )
    )
    found += [
        {
            "unit_id": r.payload["unit_id"],
            "reason": r.payload["reason"],
            "automatic": bool(r.payload.get("automatic")),
            "at": r.occurred_at.isoformat(),
        }
        for r in repairs
    ]
    return sorted(found, key=lambda item: str(item["at"]), reverse=True)


def rights_report(connection: Connection, today: date) -> list[dict[str, object]]:
    """The Editions that may still be protected in the country of the instance (EF-SRC-024). A
    list to read, indicative: no addition was or will be refused because of it."""
    return [
        {"edition_id": r.edition_id, "reason": r.reason}
        for r in RightsService(connection).protected(today)
    ]


def kept_in_service_report(connection: Connection) -> list[dict[str, object]]:
    """The downloads a purge let go of that the service still holds (another account may keep the
    file, or the answer was ambiguous): the admin deletes them there, then `purge --kept-ack`.
    Opaque request identifiers only, never a title."""
    setting = platform.app_setting
    rows = connection.execute(
        sa.select(setting.c.key, setting.c.value)
        .where(setting.c.key.like(KEPT_PREFIX + "%"))
        .order_by(setting.c.value, setting.c.key)
    )
    return [{"request": r.key.removeprefix(KEPT_PREFIX), "since": r.value} for r in rows]


def wal_pending_report(connection: Connection) -> list[dict[str, object]]:
    """An erasure whose log a reader still held (`purge.wal_busy`): to finalise with
    `shelfhold purge --checkpoint`, or by itself at the next night or start."""
    value = connection.execute(
        sa.select(platform.app_setting.c.value).where(platform.app_setting.c.key == WAL_PENDING_KEY)
    ).scalar()
    return [] if value is None else [{"since": value}]


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


def unverifiable_download_dirs(connection: Connection) -> list[dict[str, object]]:
    """The shelves for which the download folder Suwayomi reports could not be compared with the
    library (empty, relative, or in Suwayomi's own namespace): "not verifiable" is said, not
    silence (EF-EXP-044; 01 has no path map on the source side)."""
    table = platform.app_setting
    found: list[dict[str, object]] = []
    prefix = "switchover.dir_unverifiable."
    for row in connection.execute(
        sa.select(table.c.key, table.c.value)
        .where(table.c.key.like(prefix + "%"))
        .order_by(table.c.key)
    ):
        import json

        detail = json.loads(row.value)
        found.append(
            {"destination": row.key[len(prefix) :], "reason": detail.get("reason"),
             "path": detail.get("path", "")}
        )  # fmt: skip
    return found
